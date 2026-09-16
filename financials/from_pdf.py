"""从 PDF 装载三张表（含扫描件走 OCR）。

    from financials.from_pdf import load_pdf_statements
    S = load_pdf_statements("某公司2022年度审计报告.pdf")

## 为什么不能固定「跨几页」

同一张表在不同材料里占的页数不一样：

    贵州茅台 2025 年报   合并资产负债表  跨 3 页
    国城矿业 2022 审计报告 合并资产负债表  只占 1 页

写死「标题页往后取 2 页」会在国城矿业这种材料上把
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
#: 蓝色光标 2025 年报的财报附注（160 页以后）表格又多又密，
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

    标题法在扫描件上会失手：国城矿业第 8 页明明是母公司资产负债表，
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
        # 实测蓝色光标 2025 年报把合并资产负债表定位到了第 163 页，
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


def _fill(st: stm.StatementSet, doc: ip.PdfDocument, lo: int, hi: int) -> None:
    from . import textflow

    names = _known_names()
    for pg in doc.pages:
        if not (lo <= pg.number <= hi):
            continue

        rows_out: list[list[str]] = []
        for t in pg.usable_tables():
            rows_out.extend(t)

        # **表格抽出来的行「值列全空」时，回落到文字流解析。**
        # 现代 A 股年报的三张表没有表格结构，科目名和金额糊成一条文字流：
        #     货币资金 2,826,966,781.73 2,779,185,080.64 结算备付金拆出资金
        # pdfplumber 会返回行，但值列是空的（实测蓝色光标 2025 年报第 70 页：
        # 35 行，0 行带值）。不回落的话整张表看起来「存在但其实没有数」。
        if not any(len(r) > 2 and str(r[2] or "").strip() for r in rows_out):
            flow = textflow.parse_textflow(pg.text, names)
            if flow:
                rows_out = flow

        for row in rows_out:
            if len(row) < 4:
                continue
            lab = (row[0] or "").replace("\n", " ").strip()
            if not lab:
                continue
            f, _ = cn.identify(lab)
            raw = str(row[2] or "").strip()
            # **OCR 标出来的可疑金额一律不用。**
            # `？7, 756,942, 510.86` 若交给 `_to_number` 会被剥成
            # 775694251086 —— 差得离谱且不报错。
            v = None if raw.startswith("？") else _to_number(raw)
            st.rows.append(stm.StatementRow(label=lab, value=v, field=f, via="pdf"))
            if f and f not in st.fields and v is not None:
                st.fields[f] = v


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

    scores = assemble.page_scores(doc)
    picked = {k: assemble.pick_pages(scores, k)
              for k in ("balance", "income", "cash_flow")}
    S.warnings.append(
        "页范围按**内容**判定（" +
        "；".join(f"{k} {v[0]}–{v[-1]}页" for k, v in picked.items() if v) + "）")

    for kind, label in (("balance", "资产负债表"), ("income", "利润表"),
                        ("cash_flow", "现金流量表")):
        pages = picked[kind]
        if not pages:
            continue
        st = stm.StatementSet(name=label, source=p.name, unit=unit)
        _fill(st, doc, pages[0], pages[-1])
        st.columns = [f"第{pages[0]}—{pages[-1]}页"]
        setattr(S, kind, st)

    # 口径从**内容**推断，不写死（见 `meta.py`）。
    # 写死 scope="合并" 的时候：苏州井利那份**非上市单体审计报告**被报成合并。
    from . import meta

    # **把三张表的标签并起来判一次** —— 逐表判会让第一张判出的
    # 「未判定」挡住后面几张表的信息。
    text = " ".join(r.label for st in (S.balance, S.income, S.cash_flow)
                    if st for r in st.rows)
    if not S.gaap:
        S.gaap = meta.detect_gaap(text)
    if not S.scope:
        S.scope = meta.detect_scope(text)
    return S
