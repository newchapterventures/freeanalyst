"""从 PDF 装载三张表（含扫描件走 OCR）。

    from financials.from_pdf import load_pdf_statements
    S = load_pdf_statements("某公司2022年度审计报告.pdf")

## 为什么不能固定「跨几页」

同一张表在不同材料里占的页数不一样：

    某白酒公司 2025 年报   合并资产负债表  跨 3 页
    某上市公司 2022 审计报告 合并资产负债表  只占 1 页

写死「标题页往后取 2 页」会在某上市公司这种材料上把
**母公司资产负债表和合并利润表都混进来** —— 表达式跑得通，
数字却来自三张不同的表。这是最坏的一类错误：**静默且看起来正常**。

正确做法：从标题页开始，**直到下一张表的标题出现之前**。
"""

from __future__ import annotations

import re
from pathlib import Path

from ingest import pdf as ip
from ingest.html import _to_number

from . import canonical as cn
from . import statements as stm

#: 各表可能的标题，按优先级（合并表在前）
TITLES: dict[str, tuple[str, ...]] = {
    "balance": ("合并资产负债表", "合并财务状况表", "资产负债表"),
    "income": ("合并利润表", "合并损益表", "合并利润及利润分配表",
               "利润表", "损益表"),
    "cash_flow": ("合并现金流量表", "现金流量表"),
}

#: 所有标题的并集 —— 用来判断「下一张表从哪开始」
_ALL_TITLES = tuple(t for ts in TITLES.values() for t in ts)

#: 这些页出现标题多半是目录，不是表本身
_TOC_MARKERS = ("目录", "目    录", "第—", "第-", "…")

#: 标题前面出现这些词 → 这是主体/母公司表，不是我们找的合并表
_QUALIFIERS = ("母公司", "公司", "本部", "单体")

#: 表格开头的标志 —— 紧跟在表名后面的固定文字。
#:
#: ## 为什么用它定起始页（实测踩到）
#:
#: 原来的做法是「在含表名的页里挑数值最多的那页」。在**年报**上这会选错：
#: 某 A 股广告公司 2025 年报的财报附注（160 页以后）表格又多又密，
#: 于是合并资产负债表被定位到第 163 页，而它其实在第 70 页。
#:
#: 「编制单位」这个标志只出现在表的开头，不会出现在附注里。
_START_MARKERS = ("编制单位", "会企01表", "会合01表", "会企02表", "会合02表",
                  "会企03表", "会合03表", "会企04表", "会合04表",
                  "单位:元", "单位：元", "单位:人民币元", "单位：人民币元")


def _hit_title(text: str, titles: tuple[str, ...],
               allow_qualified: bool = False) -> bool:
    """这一页有没有这张表的标题。

    ## 为什么要看标题**前面**几个字（实测踩到）

    `母公司资产负债表` **包含** `资产负债表` 这个子串。
    只看子串的话，母公司表会被当成合并表的续页 —— 于是页范围
    从 (7,7) 变成 (7,8)，合并表和母公司表的数字**混在一起**
    而且不报错。

    所以标题前面若紧跟着「母公司 / 公司 / 本部」这类限定词，
    就不算命中（除非显式允许）。
    """
    for t in titles:
        idx = text.find(t)
        while idx >= 0:
            prefix = text[max(0, idx - 6):idx]
            if allow_qualified or not any(q in prefix for q in _QUALIFIERS):
                return True
            idx = text.find(t, idx + 1)
    return False


def _is_toc(page_text: str) -> bool:
    return any(m in page_text for m in _TOC_MARKERS)


#: 目录条目：`（一）合并资产负债表⋯  第6页`
_TOC_ENTRY = re.compile(
    r"(合并资产负债表|母公司资产负债表|合并财务状况表|资产负债表"
    r"|合并利润表|母公司利润表|合并损益表|利润表|损益表"
    r"|合并现金流量表|母公司现金流量表|现金流量表)"
    r"[\s⋯…•·.、\-—]*第\s*(\d+)\s*页"
)


def toc_ranges(doc: ip.PdfDocument) -> dict[str, tuple[int, int]] | None:
    """从**目录页**读出各表的页码范围。

    ## 为什么优先用目录（实测踩到）

    标题法在扫描件上会失手：某上市公司第 8 页明明是母公司资产负债表，
    标题被 OCR 认成「股名數公司燕苎鎮债表」—— **一个能对的字都没有**。
    于是它被当成了合并表的续页，两张表的数字混在一起。

    目录是权威页号，而且**不依赖正文标题能不能被认出来**。
    审计报告和年报几乎都有目录。
    """
    entries: list[tuple[str, int]] = []
    for pg in doc.pages[:6]:                      # 目录总在前面
        for m in _TOC_ENTRY.finditer(pg.text):
            entries.append((m.group(1), int(m.group(2))))
    if len(entries) < 3:
        return None

    # 换算偏移：目录页号 → PDF 页号
    offset = None
    for pg in doc.pages:
        for nm, num in entries:
            if nm == "合并资产负债表" and nm in pg.text and not _is_toc(pg.text):
                offset = pg.number - num
                break
        if offset is not None:
            break
    if offset is None:
        return None

    # 只看合并表（母公司表跳过）
    merged = [(nm, n) for nm, n in entries if nm.startswith("合并")]
    if len(merged) < 2:
        return None

    # **结束页要用「下一条目录条目」定，不是下一张合并表。**
    # 目录顺序是 合并资产负债表(6) → 母公司资产负债表(7) → 合并利润表(8)…
    # 用「下一张合并表」当边界的话，合并资产负债表会算成第 6–7 页，
    # **把母公司表也圈进来**（实测踩到，两张表的数字混在一起）。
    order = [nm for nm, _ in entries]

    out: dict[str, tuple[int, int]] = {}
    for nm, num in merged:
        i = order.index(nm)
        start = num + offset
        end = (entries[i + 1][1] + offset - 1) if i + 1 < len(entries) else start
        for key, titles in TITLES.items():
            if nm in titles:
                out[key] = (start, max(start, end))
                break
    return out or None


def _numeric_rows(rows: list[list[str]]) -> int:
    """这一页有多少行带着数值。"""
    n = 0
    for r in rows:
        if len(r) > 2 and str(r[2] or "").strip():
            n += 1
        elif len(r) > 3 and str(r[3] or "").strip():
            n += 1
    return n


def statement_range(doc: ip.PdfDocument, kind: str) -> tuple[int, int] | None:
    """一张表实际占第几页到第几页。

    1. 在候选标题页里挑**数值最多**的那页当起点（避开目录）；
    2. 往后走，**遇到任何一张表的标题就停**；
    3. 空页也不停 —— 有的报表中间插了空白页。
    """
    titles = TITLES[kind]

    best, best_score = None, -1
    for pg in doc.pages:
        if not _hit_title(pg.text, titles):
            continue
        if _is_toc(pg.text):
            continue
        # **优先取「表名 + 编制单位」的那一页。**
        # 只看数值多少会选到财报附注（附注表格又密又多），
        # 实测某 A 股广告公司 2025 年报把合并资产负债表定位到了第 163 页，
        # 而它其实在第 70 页。
        score = sum(_numeric_rows(t) for t in pg.usable_tables())
        if any(m in pg.text for m in _START_MARKERS):
            score += 100_000
        if score > best_score:
            best, best_score = pg.number, score
    if best is None:
        return None

    end = best
    for pg in doc.pages:
        if pg.number <= best:
            continue
        # 下一张表的标题出现 → 本表到头。
        # **同一张表的续页标题也算本表**，但要排除主体/母公司表。
        hit_next = _hit_title(pg.text, _ALL_TITLES, allow_qualified=True)
        same = _hit_title(pg.text, titles)
        if hit_next and not same:
            break
        end = pg.number
    return best, end


def _known_names() -> list[str]:
    """所有已知科目名 —— 文字流解析靠它把粘在一起的科目名剥开。"""
    return [n for m in cn.MAPPINGS for n in m.names]


#: `ingest/layout.py` 抽不到标签时填的占位串。**从那边 import，不另抄一份**
#: —— 两处各写一份，改一处就会不同步。
try:
    from ingest.layout import _UNLABELED as _LABEL_PLACEHOLDER
except ImportError:                                    # pragma: no cover
    _LABEL_PLACEHOLDER = "〔此行没有标签〕"

#: 布局解析出的行里**一行都认不出科目**时就回落到文字流。
#:
#: ## 为什么判据是「可映射行为 0」而不是「真标签为 0」（实测）
#:
#: 本行 `601628`（中国人寿）2025 年报第 89 页：科目名和金额**各占一行**，
#: 于是 `ingest/layout.py` 的按行解析只能从数字行里收数字、认不出标签：
#:
#:     货币资金                 ← 这一行没有数字，攒成 pending 后没人接走
#:     # # #                   ← 这一行只有数字 → 标签列填占位符
#:
#: 结果**抽到 21 行、20 行带值，科目映射 0 个** —— 表看着取到了，
#: 其实一个科目都没认出来。
#:
#: ⚠️ **不能拿「标签列非空的行动数」当判据。** 现金流量表那几页的标签列里
#: 混着附注编号残片（`45(1)` / `11(2)` 这类），它们不是空串，
#: 会把「标签塌了」的页误判成正常页（实测：第 99/100/102 页各有 3–4 条这种残片，
#: 于是回落不触发，现金流量表只映射出 5 行）。
#: 「一行都认不出科目」才是真正说明标签列没抽到的证据。
_MIN_MAPPABLE_ROWS = 1


def _mappable_rows(rows: list[list[str]]) -> int:
    """这些行里有几行能映射成统一概念 —— 只有这些行对下游有用。"""
    n = 0
    for r in rows:
        if len(r) <= 2:
            continue
        lab = str(r[0] or "").strip()
        if not lab or lab == _LABEL_PLACEHOLDER:
            continue
        if str(r[2] or "").strip() and cn.identify(lab)[0] is not None:
            n += 1
    return n


def page_rows(pg) -> list[list[str]]:
    """这一页的表格行 —— **抽不全时回落到文字流解析**。

    两种触发情形（都是实测踩到的）：

    1. **值列全空**：现代 A 股年报的三张表没有表格结构，科目名和金额糊成
       一条文字流，`pdfplumber` 返回行但值列是空的（某 A 股广告公司 2025 年报
       第 70 页：35 行、0 行带值）。
    2. **标签列塌了**：科目名与金额**各占一行**，按行解析只收得到数字，
       于是每一行的标签列都是空/占位符（中国人寿 2025 年报第 89/90 页，
       以及中信证券年报第 176/177 页）。这种形态**每行都有值**，
       所以旧的「值列全空」判据碰不到它 —— 表看着正常，科目映射却是 0 个。
    3. **金额格垂直居中**：科目名与它的金额**不在同一条基线上**
       （实测：金额行中心比科目行**高约 7 点**，而行距 15 点）。这时按 y 从上到下
       读出来的文字流是"金额在前、科目名在后"，纯文本规则会**整表错配一位**
       （中国人寿第 89 页：`货币资金` 拿到了下一行的 50,879）。这一种只能靠**坐标**解。

    三种情形都表现为**一行都认不出科目**（见 `_MIN_MAPPABLE_ROWS`），
    所以判据统一成这一条。

    回落**不能更差**：每一种都只在**可映射行更多**时才采用，
    否则保留原样（宁可照旧，也不要拿更碎的结果换掉能用的结果）。
    """
    from . import textflow

    rows: list[list[str]] = []
    for t in pg.usable_tables():
        rows.extend(t)

    if _mappable_rows(rows) >= _MIN_MAPPABLE_ROWS:
        return rows

    # 按坐标配对（情形 3）—— **只在这页确实是"标签与金额分成两行"的版式时才试**。
    # 判据量的是**版式本身**（纯金额行占比），不是"结果好不好"：
    # 错配也会映射出更多行，用行数当质量信号会放过错配（真踩过，见 layout 里的说明）。
    coord: list[list[str]] = []
    try:
        from ingest import layout as _layout

        if _layout.looks_like_split_rows(pg.words()):
            coord = _layout.parse_words_rows(pg.words())
    except Exception:                                # noqa: BLE001
        coord = []                                   # 坐标取不到就跳过，不影响别的路
    if coord and _mappable_rows(coord) > _mappable_rows(rows):
        rows = coord

    if _mappable_rows(rows) >= _MIN_MAPPABLE_ROWS:
        return rows

    flow = textflow.parse_textflow(pg.text, _known_names())
    if flow and _mappable_rows(flow) > _mappable_rows(rows):
        return flow
    return rows


def _fill(st: stm.StatementSet, doc: ip.PdfDocument, lo: int, hi: int,
          kind: str = "") -> None:
    """把 `lo`–`hi` 页里属于这张表的行填进来。

    `kind` 是 `balance` / `income` / `cash_flow` —— 用来**挡住串表**。
    留空则不挡（老行为）。
    """
    #: 每个字段见过的所有取值 —— `(值, 页码, 该页是不是合并报表)`
    seen: dict[object, list[tuple[float, int, bool]]] = {}
    #: 被挡掉的（别的表的）科目，按字段计数 —— 只报数，不刷屏
    foreign: dict[str, int] = {}

    for pg in doc.pages:
        if not (lo <= pg.number <= hi):
            continue

        # 这一页像不像**合并**报表 —— 母公司表不会有「归属于母公司」「少数股东权益」
        consolidated = ("归属于母公司" in pg.text or "少数股东权益" in pg.text)

        rows_out: list[list[str]] = page_rows(pg)

        for row in rows_out:
            if len(row) < 4:
                continue
            lab = (row[0] or "").replace("\n", " ").strip()
            if not lab:
                continue
            f, _ = cn.identify(lab)
            # **挡住串表。** 页范围可能扫到相邻那张表的尾巴（实测第 61 页
            # 混进母公司资产负债表的所有者权益，差 750 亿）。
            if f is not None and kind:
                owner = cn.field_statement(f)
                if owner not in (kind, cn.ANY):
                    foreign[f"{owner}:{getattr(f, 'value', f)}"] = (
                        foreign.get(f"{owner}:{getattr(f, 'value', f)}", 0) + 1)
                    continue
            raw = str(row[2] or "").strip()
            # **OCR 标出来的可疑金额一律不用。**
            # `？7, 756,942, 510.86` 若交给 `_to_number` 会被剥成
            # 775694251086 —— 差得离谱且不报错。
            v = None if raw.startswith("？") else _to_number(raw)
            st.rows.append(stm.StatementRow(label=lab, value=v, field=f, via="pdf"))
            if f and v is not None:
                seen.setdefault(f, []).append((v, pg.number, consolidated))
                if f not in st.fields:
                    st.fields[f] = v

    if foreign:
        items = "、".join(f"{k.split(':')[1]}×{n}" for k, n in
                          sorted(foreign.items(), key=lambda x: -x[1])[:6])
        st.conflicts.append(
            f"页范围里有 {sum(foreign.values())} 行属于**别的表**，已挡掉（{items}）。"
            f"—— 说明页范围划宽了，扫到了相邻表的尾巴。")

    _resolve_conflicts(st, seen)


#: 资产总计的**各种键**：`seen` 的键有时是 `Field` 枚举、有时是字符串
#: （实测某 A 股审计报告就是字符串，导致规模法完全没生效、退化成"未能按规模比较" ✗）。
_TOTAL_ASSETS_KEYS = ("资产总计", "资产合计", "总资产", "Total Assets")
#: 资产总计不足以比较两页时的**退路** —— 它们与规模同向（合并那页都更大）。
#: ⚠️ **只在同一字段内比较**，绝不拿"资产"和"负债"互相排大小。
_SCALE_FALLBACK_KEYS = ("负债合计", "Total Liabilities", "所有者权益合计",
                        "股东权益合计", "Total Equity",
                        "负债和所有者权益总计", "Total Liabilities and Equity")


def _is_total_assets(f: object) -> bool:
    """这个键是不是「资产总计」？**枚举与字符串都认**。"""
    if f is cn.Field.TOTAL_ASSETS:
        return True
    return getattr(f, "value", f) in _TOTAL_ASSETS_KEYS


def _page_scale(seen: dict[object, list[tuple[float, int, bool]]]
                ) -> tuple[dict[int, float], str]:
    """每页的「规模」+ **用的是哪个字段**。返回 `(页码→规模, 字段名)`。

    用途见 `_resolve_conflicts`。取绝对值，所以被印成负数也不影响比较。

    ## 为什么要退路（实测）
    某 A 股审计报告的**资产总计只在一页上抽到**（另一页没抽到）→ 比较不了 → 规则不生效。
    所以依次试「资产总计」→「负债合计」→「所有者权益合计」等，
    **哪一个字段自己覆盖了两页以上，就用它**。
    一个字段覆盖不了就换下一个，**永不跨字段混比**（拿资产和负债排大小是没意义的）。

    **字段名一并返回**：提示里要说"取**X**最大那一页的"，说错字段等于编依据。
    """
    for keys in (_TOTAL_ASSETS_KEYS, _SCALE_FALLBACK_KEYS):
        for f, pairs in seen.items():
            name = getattr(f, "value", f)
            if name not in keys:
                continue
            m: dict[int, float] = {}
            for v, pg, _flag in pairs:
                m[pg] = max(m.get(pg, 0.0), abs(v))
            if len(m) >= 2:
                return m, str(name)
    return {}, ""


def _resolve_conflicts(st: stm.StatementSet,
                       seen: dict[object, list[tuple[float, int, bool]]]) -> None:
    """同一个字段出现多个不同取值时的裁决。

    ## 为什么必须做（实测踩到，金额差 750 亿）

    某白酒公司年报里，`所有者权益(或股东权益)合计` 出现两次：

        第 59页  合并资产负债表        253,959,253,909.07
        第 61页  母公司资产负债表的尾部   178,999,246,453.61

    而利润表的页范围是 61–64 —— **第 61 页还残留着母公司资产负债表的尾巴**，
    于是 `_fill` 把它当利润表的数据吃了进去。

    原来的代码是 `if f not in st.fields` —— **第一个值赢，静默**。
    出来一个 1790 亿的「所有者权益」，而真值是 2540 亿。
    **勾稽不会不平**，因为这个字段根本不参与勾稽。

    ## 裁决规则：**按规模取自哪一页**（原先只是"取首次出现"）
    同一科目在多页有值时，用**该页的资产总计**当尺子：合并 = 母公司 + 子公司 − 抵销，
    所以合并那一页的资产总计**通常**更大。取最大那一页的值。

    ⚠️ 这是**量级证据**，不是页序约定。"中文年报里合并表永远排在母公司表前面"
    只是**惯例** —— 拿到排反的材料就不成立，而且它**不成文、不可核对**。
    也**不是**"按页判断是不是合并报表"（那条真试过、真失败过，见上）。

    比不了规模时（那一页没取到资产总计）**退回首次出现，并明说没能比较** ——
    不装作有依据。

    另一个值**不丢**：报出来，让人能核对。
    """
    scale, scale_name = _page_scale(seen)
    big_page = max(scale, key=lambda p: scale[p]) if len(scale) >= 2 else None

    for f, pairs in seen.items():
        vals = {round(v, 2) for v, _, _ in pairs}
        if len(vals) <= 1:
            continue

        chosen = pairs[0]
        if big_page is not None:
            on_big = [p for p in pairs if p[1] == big_page
                      and round(p[0], 2) != round(chosen[0], 2)]
            if on_big:
                chosen = on_big[0]
                why = (f"取**{scale_name}最大那一页**的（通常即合并口径，"
                       f"第 {big_page} 页{scale_name}最大）")
            else:
                why = (f"取**首次出现**的（第 {big_page} 页{scale_name}最大，"
                       "与首次出现一致）")
        else:
            why = "取**首次出现**的（**未能按规模比较**，请核对是否合并口径）"

        st.fields[f] = chosen[0]
        # **另一个值不丢** —— 必须从**全部**候选里排除选中那个，
        # 不能用 `pairs[1:4]`：选中项不再必然是第一个（规模法可能选后面那个页），
        # 那样会把"第一个值"整条漏掉（这个坑真踩过，被测试抓到）。
        rest = [b for b in pairs if round(b[0], 2) != round(chosen[0], 2)]
        others = "；".join(f"{b[0]:,.2f}（第 {b[1]} 页）" for b in rest[:3])
        if len(rest) > 3:
            others += f"…（另有 {len(rest) - 3} 个）"
        st.conflicts.append(
            f"「{getattr(f, 'value', str(f))}」出现 {len(vals)} 个取值，"
            f"{why} {chosen[0]:,.2f}（第 {chosen[1]} 页）。其余：{others}")


def evidence_text(page_text: str, statements, toc_text: str = "") -> str:
    """判会计准则 / 口径用的证据文本。

    ## 一、标题在**页面正文**里，不在行标签里（实测踩到）

    以前这里只用 `r.label`（行标签）：行标签里有「资产总计」「短期借款」，
    却没有「合并资产负债表」这个**表标题** —— 而口径恰恰是标题说了算。
    于是 176 页的上市公司年报因为标题读不到，被兜底判成「单体」，
    而它的三张表其实是**合并**口径。

    ## 二、正文表头本身也可能是乱的，所以还要带**目录**（实测踩到）

    同一份报告里，正文表头的文字层抽出来是错的：

        真标题「中期合并资产负债表」  →  抽出来「中期公司资产负债表」（少了「合并」）
        「四、财务报表」                →  抽成「表报务财四、」（字符顺序乱了）

    目录里的写法却是规范的（`中期合并资产负债表109`）。所以证据要三层：
    目录 + 表所在页正文 + 行标签 —— 少一层就可能判错，而**口径错是无声的**。
    """
    labels = " ".join(r.label for st in statements if st for r in st.rows)
    return f"{toc_text} {page_text} {labels}"


def load_pdf_statements(path: str | Path, unit: str = "元") -> stm.Statements:
    """从一份 PDF（年报 / 审计报告）装载三张表。"""
    p = Path(path)
    doc = ip.extract_pdf(p)

    S = stm.Statements(gaap="", scope="", audited="已审计",
                       period="", warnings=list(doc.warnings))
    if doc.ocr_pages:
        S.audited = "已审计（全文走 OCR，数字需人工复核）"

    # **按内容组装**（`assemble.py`）：不看页码、不靠标题。
    # 原来的「标题 + 往后取几页」走过三次补丁，12 家公司里 8 家失败 ——
    # 标题在目录/正文/附注/交叉引用里反复出现，附注又有 200+ 页表格，
    # 任何位置启发式都会被带偏。
    from . import assemble

    picked, notes = assemble.pick_all(doc)
    S.warnings.extend(notes)
    S.warnings.append(
        "页范围按**内容**判定（" +
        "；".join(f"{k} {v[0]}–{v[-1]}页" for k, v in picked.items() if v) + "）")

    # **单位与期间：只有装载器拿得到。**
    #
    # 调用方从文件字节里 sniff 是认不出来的 —— PDF 的文字层在压缩流里。
    # 实测三份真实材料（H 股年报、非上市审计报告、A 股扫描件），
    # **单位全部认不出**，而它们的报表头上就写着「人民币千元」「金额单位：元」——
    # 那几行正躺在这里读过的页里。认错单位是 1000 倍级的静默错误，
    # 所以认不出就留空、由调用方停下来问人，而不是默认给「元」。
    from . import meta

    page_text = " ".join(
        pg.text for pg in doc.pages
        if any(pg.number in v for v in picked.values() if v))
    # ★ **正表页首说的单位，优先于调用方的预扫描**（实测踩到，差 100 万倍）
    #
    # 原先这里是 `if not unit:` —— 于是调用方先认过就轮不到正表说话了。
    # 而调用方（`intake`）认的是**整份文件的前 60,000 字**：中国人寿那份报表在
    # 第 89 页以后，它在前 40 页里撞到某处「单位：元」就判成了「元」，
    # 而正表页首写的是「金额单位为人民币百万元」✗ —— 差 **100 万倍**，
    # 且数字形状完全正常，肉眼看不出来（货币资金显示成 50,879「元」，
    # 实际是 508.79 亿）。中国平安那份同样中招。
    #
    # 上面那段注释早就写了「调用方从文件字节里 sniff 是认不出来的」——
    # 这里只是把那条原则真正落实：**正表认得出就用正表的**，
    # 并且把分歧**写进警告**，不静默改。
    u, basis = meta.detect_unit(page_text)
    if u and u != unit:
        if unit:
            S.warnings.append(
                f"金额单位：**以正表页首的「{u}」为准**（依据：{basis}）。"
                f"预扫描认的是「{unit}」，两者不一致 —— 正表优先，"
                f"因为预扫描只看得到文件开头几十页。")
        else:
            S.warnings.append(f"金额单位从报表正文认出：{u}（依据：{basis}）")
        unit = u
    S.unit = unit
    if not S.period:
        S.period = meta.detect_period(page_text)

    for kind, label in (("balance", "资产负债表"), ("income", "利润表"),
                        ("cash_flow", "现金流量表")):
        pages = picked[kind]
        if not pages:
            continue
        st = stm.StatementSet(name=label, source=p.name, unit=unit)
        _fill(st, doc, pages[0], pages[-1], kind=kind)
        st.columns = [f"第{pages[0]}—{pages[-1]}页"]
        setattr(S, kind, st)
        # **字段冲突要浮到顶层。** 这类错不会让勾稽不平，只在报告里显示才有用。
        for c in st.conflicts:
            S.warnings.append(f"[{label}] {c}")

    # **行业判定已退回**（判据不成立，见 `meta._FIN_SIGNS` 的说明）：
    # 试过"金融科目出现在报表行里"就判金融 —— 实测矿业公司的三张表行里
    # 也有「吸收存款」「保险责任准备金」这些科目（集团旗下有金融/保险业务），
    # 会把工商业材料误判成金融 ✗（四个参考值被全部关掉，比不改更坏）。

    # 口径从**内容**推断，不写死（见 `meta.py`）。
    # 写死 scope="合并" 的时候：某非上市公司那份**非上市单体审计报告**被报成合并。
    from . import meta

    # **把三层证据并起来判一次**：目录 + 表所在页正文 + 行标签。
    # 逐表判会让第一张判出的「未判定」挡住后面几张表的信息；
    # 只给标签会漏掉表标题；只给表页正文又会被乱掉的文字层带偏（见 evidence_text）。
    toc_text = " ".join(pg.text for pg in doc.pages[:8])
    text = evidence_text(page_text, (S.balance, S.income, S.cash_flow),
                         toc_text=toc_text)
    if not S.gaap:
        S.gaap = meta.detect_gaap(text)
    if not S.scope:
        S.scope = meta.detect_scope(text)

    # **折旧摊销必须从附注取。** A 股主表现金流量表没有间接法调节段，
    # 调节表只在「补充资料」里 —— 拿不到 D&A 就算不出 EBITDA。
    from . import notes

    S.da = notes.extract_da(doc)
    if S.cash_flow is not None and S.da.total is not None:
        S.cash_flow.fields.setdefault(cn.Field.DEPRECIATION_AMORTIZATION,
                                      S.da.total)
    return S
