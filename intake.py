"""intake —— 从**一个材料目录**到一份跑得动的估值报告。

## 它接的是哪一条断链

`value.py` 是配置驱动的：跑一次估值，得先有人写一份 JSON —— 哪张表在哪个
文件、每个假设填多少。**这份 JSON 的形状是给程序看的，不该由人写。**

更硬的一处：**PDF 和 Excel 材料根本没有通路。** 配置里的 `statements` 节
只认「一张表一个文件」的 HTML（`statements.load_one()` 只走 HTML 抽取）；
PDF 和 Excel 各有自己的装载器（`from_pdf` / `from_excel`），一次出一整套
三张表，形状上就进不了那个配置节。实测后果：三类材料里有两类的解析结果
**走不到估值**。

intake 做三件事，把断链接上：

    1  认材料   扫目录，判断哪份文件是哪张表（**复用已有装载器，不重写**）
    2  列问题   把必须由人给的假设列成一份可填的清单（**不代填**）
    3  拼配置   事实类自动填、假设类留空并标成缺口，交给 `value.run_report`

## 三条不能破的界线

**一、事实自动填，假设绝不自动填。**
历史比率只作为参考资料出现在问答清单的**注释**里。把历史 EBITDA 率自动
填成预测假设，用户会以为那是自己的判断 —— 那正是这个产品要避免的事。

**二、认不出来就说认不出来。**
没认出来的文件、判不出的单位、算不出的口径，全部列出来。少了材料会让后面
的问答用「材料里没写」来解释这个缺失，而实际上是根本没读进来。

**三、单位没确定就不算。**
报表是千美元、引擎默认万元 —— 猜错是 1000 倍级的静默错误。
认不出单位时停下要你声明，不替你做主。

## 用法

    python3 freeanalyst.py appraise <材料目录>              # 第一遍：认材料 + 出问答清单
    python3 freeanalyst.py appraise <材料目录> --answers a.txt  # 第二遍：出报告

    python3 intake.py <材料目录>                            # 也可以单独跑
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:                                      # 只为类型检查，运行时不导入
    from financials import statements as stm

#: 扩展名 → 材料类型。
EXT_KIND = {
    ".pdf": "pdf",
    ".htm": "html", ".html": "html",
    ".xls": "excel", ".xlsx": "excel",
    ".txt": "text", ".md": "text",
}

#: 三种表的显示名。
LABEL = {"balance": "资产负债表", "income": "利润表", "cash_flow": "现金流量表"}

#: 一份 PDF 里三张表加起来映射少于这个行数，就认为「这份 PDF 不是正表」。
#: 实测：10-K 的封面页 / 附注节也能映射出几行，但远达不到正表的量。
_MIN_PDF_MAPPED = 8


# ─────────────────────────── 材料扫描 ───────────────────────────

@dataclass
class Candidate:
    """一个候选文件，以及它像哪张表。"""

    path: Path
    kind: str            # balance / income / cash_flow / unknown
    mapped: int = 0      # 映射上的行数
    rows: int = 0        # 总行数

    @property
    def rate(self) -> float:
        return self.mapped / self.rows if self.rows else 0.0

    def render(self) -> str:
        k = LABEL.get(self.kind, "认不出")
        return (f"{self.path.name} → {k}"
                f"（映射 {self.mapped}/{self.rows} 行）")


@dataclass
class Materials:
    """一个材料目录（**或单独一份文件**）里认出来的东西。"""

    directory: Path
    statements: "stm.Statements | None" = None    # financials.statements.Statements
    unit: str = ""
    unit_basis: str = ""
    source: str = ""                          # pdf / excel / html
    detected: dict[str, str] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    unused: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return self.statements is not None and bool(self.detected)

    @property
    def is_file(self) -> bool:
        return self.directory.is_file()

    @property
    def label(self) -> str:
        """给这份材料起个名字 —— 目录名，或者（单文件时）文件名去后缀。

        **真实材料常常就是一份 PDF**，不是整整齐齐一个目录。
        这时候拿整个文件名当标的名，报告封面会变成
        「某公司2024年审计报告.pdf」，所以去掉后缀。
        """
        return self.directory.stem if self.is_file else self.directory.name

    def render(self) -> str:
        head = "材料文件" if self.is_file else "材料目录"
        out = [f"{head}　{self.directory}"]
        if self.source:
            out.append(f"  采用的来源　{self.source}")
        for kind in ("balance", "income", "cash_flow"):
            name = self.detected.get(kind)
            c = next((x for x in self.candidates
                      if x.kind == kind and name and x.path.name == name), None)
            if c is not None:
                out.append("  ✓ " + c.render())
            else:
                out.append(f"  ✗ {LABEL[kind]} —— 没认出来")
        out.append(f"  单位　{self.unit or '**没认出来（必须你声明）**'}"
                   + (f"　依据：{self.unit_basis}" if self.unit_basis else ""))
        if self.statements is not None:
            s = self.statements
            out.append(f"  口径　{s.gaap or '未判定'} · {s.scope or '未判定'} · "
                       f"{s.audited or '未标注'} · {s.period or '期间未标'}")
        if self.unused:
            out.append("  没用上的文件（**不是静默跳过**）：")
            for u in self.unused:
                out.append(f"    · {u}")
        # 映射率过低：把"表没读懂"顶到扫描结果里，别让人以为材料本来就缺数。
        if self.statements is not None:
            for w in self.statements.mapping_warnings():
                out.append("  " + w)
        for n in self.notes:
            out.append(f"  注：{n}")
        return "\n".join(out)


# 单位提示的判据在 `financials/meta.py`（`detect_unit`）—— 那边装载器也在用，
# 全项目只有一份，免得两边认出来的单位不一样。


def detect_unit(text: str) -> tuple[str, str]:
    """从材料文本里认金额单位。返回 `(单位, 依据)`，认不出返回 `("", "")`。

    **判据只有一份**，在 `financials/meta.py`：装载器（`from_pdf`）也在用同一份。
    两边各判一次是最坏的结果 —— 一边认出「千美元」、另一边认成「元」，
    差了 1000 倍而且看不出来。
    """
    from financials import meta

    return meta.detect_unit(text)


def _read_text(path: Path, limit: int = 400_000) -> str:
    """尽量读出文件的纯文本（HTML 去标签、PDF 取文字层）。读不出就返回空串。

    ## PDF 不能当字节解码（实测踩到）

    这条以前只做「读字节 → 试几种编码」，对 PDF 就是一堆
    `%PDF-1.6 /Filter/FlateDecode ... stream` 二进制乱码。
    而**口径判定（合并 / 单体、准则）就跑在这堆乱码上**：
    实测一份 176 页的上市公司年报，因为读不到「合并资产负债表」这个标题，
    被兜底判成「单体」—— 而它的三张表其实是合并口径。

    口径错比数字错危险：数字错了勾稽会不平，口径错了无声无息。

    读不出来时**返回空串**，不要返回乱码：空串会让口径判成「未判定」，
    乱码会让它判出一个看起来很具体的错结论。
    """
    if path.suffix.lower() == ".pdf":
        try:
            from ingest import pdf as ip
            doc = ip.extract_pdf(path)
            return " ".join((pg.text or "") for pg in doc.pages)[:limit]
        except Exception:                              # noqa: BLE001
            return ""
    try:
        raw = path.read_bytes()[:limit]
    except OSError:
        return ""
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            t = raw.decode(enc, errors="ignore")
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return ""
    # 二进制伪装成文本（比如后缀写错、或没走的 PDF 分支）：宁可当"读不出"
    if t.count("\x00") > 40 or t.lstrip()[:5] == "%PDF-":
        return ""
    if path.suffix.lower() in (".htm", ".html"):
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        t = re.sub(r"&nbsp;?", " ", t)
    return t


#: 从文本里抓一个四位年份 —— 估值基准日/期间都可能带。
_YEAR = re.compile(r"(?:19|20)\d{2}")


def _labels(st) -> str:
    return " ".join(r.label for r in st.rows)


#: 英文材料的表名标志 —— 中文标志（`assemble`）对 10-K 这类材料完全失效。
_EN_MARK = {
    "balance": ("total assets", "total current assets", "total liabilities",
                "stockholders' equity", "shareholders' equity", "total equity"),
    "income": ("total revenue", "net revenue", "revenue", "cost of revenue",
               "gross profit", "operating income", "net income", "income tax"),
    "cash_flow": ("cash flows from operating", "operating activities",
                  "investing activities", "financing activities",
                  "cash and cash equivalents at"),
}


def classify(labels: str, rows: int = 0) -> str:
    """这份材料像哪张表。中文走 `assemble.signature`，英文补一套标志。"""
    from financials import assemble as asm

    sig = asm.signature(labels, rows)
    best_cn = max(sig, key=lambda k: sig[k]) if sig else None
    cn_score = sig.get(best_cn, 0) if best_cn else 0

    low = labels.lower()
    en = {k: sum(1 for m in ms if m in low) for k, ms in _EN_MARK.items()}
    best_en = max(en, key=lambda k: en[k]) if en else None
    en_score = en.get(best_en, 0) if best_en else 0

    if cn_score >= 100:
        return best_cn
    if en_score >= 3:
        return best_en
    if cn_score and not en_score:
        return best_cn
    return "unknown"


def _best_pdf(paths: list[Path], unit: str,
              ) -> tuple["stm.Statements | None", Candidate | None, list[str]]:
    """在若干 PDF 里挑最像正表的那一份。"""
    from financials.from_pdf import load_pdf_statements

    best, best_c = None, None
    unused: list[str] = []
    for p in paths:
        try:
            S = load_pdf_statements(p, unit)
        except Exception as exc:                      # noqa: BLE001
            unused.append(f"{p.name}：读不了（{type(exc).__name__}: {exc}）")
            continue
        mapped = sum(sum(1 for r in st.rows if r.field is not None)
                     for st in (S.balance, S.income, S.cash_flow) if st)
        rows = sum(len(st.rows) for st in (S.balance, S.income, S.cash_flow) if st)
        c = Candidate(p, "pdf", mapped, rows)
        kinds = [k for k in ("balance", "income", "cash_flow")
                 if getattr(S, k) is not None]
        if mapped >= _MIN_PDF_MAPPED and len(kinds) >= 2:
            if best is None or mapped > best_c.mapped:      # type: ignore[union-attr]
                if best is not None:
                    unused.append(f"{best_c.path.name}：三张表映射更少，未采用")  # type: ignore[union-attr]
                best, best_c = S, c
            else:
                unused.append(f"{p.name}：三张表映射更少，未采用")
        else:
            unused.append(f"{p.name}：三张表只认出 {len(kinds)} 张"
                          f"／映射 {mapped} 行 —— 判断这不是正表")
    return best, best_c, unused


def _scan_html(paths: list[Path], unit: str,
               ) -> tuple["stm.Statements | None", list[Candidate],
                          dict[str, str], list[str]]:
    """HTML 材料：**一张表一个文件**，逐份判它是哪张表。"""
    from financials import statements as stm

    cands: list[Candidate] = []
    unused: list[str] = []
    for p in paths:
        try:
            st = stm.load_one(p, p.stem, unit)
        except Exception as exc:                      # noqa: BLE001
            unused.append(f"{p.name}：读不了（{type(exc).__name__}: {exc}）")
            continue
        mapped = sum(1 for r in st.rows if r.field is not None)
        kind = classify(_labels(st), len(st.rows))
        if kind == "unknown" or not mapped:
            unused.append(f"{p.name}：认不出是哪张表（映射 {mapped}/{len(st.rows)} 行）")
            continue
        cands.append(Candidate(p, kind, mapped, len(st.rows)))

    detected: dict[str, str] = {}
    picked: dict[str, Candidate] = {}
    for kind in ("balance", "income", "cash_flow"):
        pool = [c for c in cands if c.kind == kind]
        if not pool:
            continue
        pool.sort(key=lambda c: (-c.mapped, -c.rate))
        win = pool[0]
        picked[kind] = win
        detected[kind] = win.path.name
        for other in pool[1:]:
            unused.append(f"{other.path.name}：也像{LABEL[kind]}，"
                          f"但映射更少（{other.mapped} 行），未采用")

    if not picked:
        return None, cands, {}, unused

    S = stm.Statements(gaap="", scope="", audited="未标注", period="")
    #: **表标题也在文件里，一起读进来判。**
    #: 只拿行标签判会让标题那一行漏掉 —— 实测一份 10-K 的合并资产负债表
    #: 被 meta 判成「单体」（行标签里没有 consolidated，标题里有）。
    #: 口径报错比数字报错危险：数字错了勾稽会不平，口径错了无声无息。
    raw = " ".join(_read_text(c.path, 20_000) for c in picked.values())
    for kind, win in picked.items():
        st = stm.load_one(win.path, LABEL[kind], unit)
        setattr(S, kind, st)
        if not S.period:
            from financials import meta
            S.period = meta.detect_period(raw)
    from financials import meta
    text = raw + " " + " ".join(
        _labels(st) for st in (S.balance, S.income, S.cash_flow) if st)
    # **装载器判出来的口径不覆盖**：`from_pdf` 手里有"三张表所在页的正文"，
    # 那里才有「合并资产负债表」这个标题；这里只有文件前 2 万字（封面/目录）。
    # 以前这里无条件覆盖，于是好端端的「合并」被这层的兜底值改成「单体」。
    if not S.gaap:
        S.gaap = meta.detect_gaap(text)
    if not S.scope:
        S.scope = meta.detect_scope(text)
    if not S.unit:
        S.unit = unit
    return S, cands, detected, unused


def _scan_excel(paths: list[Path], unit: str,
                ) -> tuple["stm.Statements | None", list[str]]:
    from financials.from_excel import load_excel_statements

    try:
        S = load_excel_statements(paths, unit)
    except Exception as exc:                          # noqa: BLE001
        return None, [f"Excel 读不了：{type(exc).__name__}: {exc}"]
    return S, []


def scan(materials: str | Path, unit: str = "") -> Materials:
    """扫一份材料 —— **一个目录，或者单独一份文件** —— 认出三张表。

    `unit` 显式给了就用它；没给就自己认，认不出返回空 —— 不猜。

    ## 为什么单文件也要支持（实测踩到）

    真实材料常常就是一份 PDF（拿到的是一份审计报告，不是一个整理好的目录），
    只吃目录的话第一句话就把人挡在门外了。单文件时把它当成"只有一份材料的
    集合"处理，其余逻辑完全一样。
    """
    d = Path(materials).expanduser()
    if d.is_file():
        files = [d]
    elif d.is_dir():
        files = sorted(
            p for p in d.rglob("*")
            if p.is_file() and not p.name.startswith(".")
            and p.suffix.lower() in EXT_KIND
        )
    else:
        raise FileNotFoundError(f"既不是文件也不是目录：{d}")
    mat = Materials(directory=d)
    pdfs = [p for p in files if EXT_KIND[p.suffix.lower()] == "pdf"]
    excels = [p for p in files if EXT_KIND[p.suffix.lower()] == "excel"]
    htmls = [p for p in files if EXT_KIND[p.suffix.lower()] == "html"]
    texts = [p for p in files if EXT_KIND[p.suffix.lower()] == "text"]

    # ---------- 单位：先认，认不出就留空 ----------
    if unit:
        mat.unit, mat.unit_basis = unit, "你指定的"
    else:
        sniff, sniff_name = "", ""
        for p in (htmls + pdfs + texts)[:6]:
            t = _read_text(p, 60_000)
            if detect_unit(t)[0]:
                sniff, sniff_name = t, p.name
                break
            if not sniff:
                sniff, sniff_name = t, p.name
        u, basis = detect_unit(sniff)
        mat.unit, mat.unit_basis = u, (f"{sniff_name}｜{basis}" if u else "")

    # ---------- 三张表：PDF 优先，其次 Excel，最后 HTML ----------
    if pdfs:
        S, c, unused = _best_pdf(pdfs, mat.unit)
        mat.unused.extend(unused)
        if S is not None:
            mat.statements, mat.source = S, "PDF"
            # 单位／期间：装载器读正文时已经判过 —— 比从文件字节 sniff 可靠得多。
            # 实测三份真实 PDF（H 股年报、非上市审计报告、A 股扫描件）
            # 全都只能靠这一条认出来。
            if not mat.unit:
                u = getattr(S, "unit", "")
                if u:
                    mat.unit = u
                    mat.unit_basis = "报表正文（装载器判定）"
            mat.candidates = [c] if c else []
            for kind in ("balance", "income", "cash_flow"):
                st = getattr(S, kind)
                if st is not None:
                    pdf = c.path if c else d
                    mat.detected[kind] = getattr(st, "source", "") or pdf.name
                    mat.candidates.append(Candidate(
                        pdf, kind,
                        sum(1 for r in st.rows if r.field is not None),
                        len(st.rows)))
    if mat.statements is None and excels:
        S, unused = _scan_excel(excels, mat.unit)
        mat.unused.extend(unused)
        if S is not None:
            mat.statements, mat.source = S, "Excel"
            for kind in ("balance", "income", "cash_flow"):
                st = getattr(S, kind)
                if st is not None:
                    nm = getattr(st, "source", "")
                    mat.detected[kind] = nm
                    mat.candidates.append(Candidate(
                        d / nm, kind,
                        sum(1 for r in st.rows if r.field is not None),
                        len(st.rows)))
    if mat.statements is None and htmls:
        S, cands, detected, unused = _scan_html(htmls, mat.unit)
        mat.unused.extend(unused)
        mat.candidates.extend(cands)
        if S is not None:
            mat.statements, mat.source = S, "HTML"
            mat.detected = detected

    # **单位：所有检测都走完了还没有，才说要用户自己声明。**
    # 这一条放在最后（PDF 那条路要等装载器读过正文才可能认出来）——
    # 早放会出现「注释说没认出来、上面一行却显示已认出千元」的自相矛盾。
    if not mat.unit:
        mat.notes.append("金额单位没认出来 —— 报表可能是千元/千美元，"
                         "引擎默认万元，差 1000 倍。请用 --unit 声明，"
                         "或在问答清单里把 unit 填上")

    if texts:
        mat.notes.append(f"{len(texts)} 份文本材料（CIM／纪要）不进三张表，"
                         "走 `freeanalyst.py ingest` 建索引后由 `ask` 用")
    return mat


# ─────────────────────── 问答清单（人给的部分） ───────────────────────

@dataclass
class Q:
    """一个必须由人回答的问题。

    ## options 与 suggest：**能确定的就不要人打字**

    `options` 是**封闭集合**（买方/卖方/中立、合并/单体……）→ 界面上做成下拉，只能选。
    `suggest` 是**建议值**（元/千元/万元……、CNY/USD/HKD……）→ 做成可编辑下拉：
    能点选，也能写清单里没有的那个。

    判断标准很简单：**这个答案是不是从有限几种里挑一个**。是就用 options；
    若有限但不封闭（单位、货币会冒出新的），用 suggest。两者都不用，才留给手打。
    """

    key: str
    label: str
    hint: str = ""
    default: str = ""
    reference: str = ""
    group: str = "假设"
    #: 封闭选项：界面上必须做成下拉，不许手打
    options: tuple[str, ...] = ()
    #: 建议值：界面上做成"可编辑下拉"，能点选也能自行填写
    suggest: tuple[str, ...] = ()
    #: 写法/单位：`"%"` = 界面上以百分数显示（框里带 %，人只填数值）。
    #: 引擎拿到的字符串**必须自带单位** —— 光一个 `8.5` 会被当成 850%（100 倍级静默错误）。
    unit: str = ""
    #: 这个数**从哪来**（见 ORIGINS）。界面上会标出来，txt 清单里也会写。
    #: 不标来源的字段，等于让人分不清"报表里有的"和"公司之外要我自己找的"。
    origin: str = ""

    def line(self) -> str:
        pad = " " * max(1, 24 - len(self.key))
        tail = f"　# {self.label}"
        if self.reference:
            tail += f"　｜ 参考：{self.reference}"
        if self.hint:
            tail += f"　｜ {self.hint}"
        if self.options:
            tail += f"　｜ 可选：{' / '.join(self.options)}"
        if self.unit == "%":
            # 这句会写进 txt 清单，所以必须说**txt 那条路的**规矩：
            # 那边没有"只填数值就补 %"的规则（那是网页向导的便利），
            # 照网页的说法写 `8.5` 会变成 850%。
            tail += "　｜ 按百分数填：写 8.5% 或 0.085（不要只写 8.5）"
        elif self.unit:
            tail += f"　｜ 单位：{self.unit}"
        if self.origin:
            tail += f"　｜ 【{self.origin}】"
        return f"{self.key}{pad}= {self.default}{tail}"


#: 字段的**来源类别** —— 界面上要标出来。
#: 不标，人就分不清"报表里有的"（工具必须给）与"公司之外要我自己找的"（工具不该编）。
ORIGIN_FILING = "财报"        # 报表里直接有的数
ORIGIN_DERIVED = "财报推算"    # 按报表推出来的，口径要人确认
ORIGIN_EXTERNAL = "外部"       # 公司之外的市场信息，由用户提供
ORIGIN_JUDGMENT = "判断"       # 用户对未来的判断，报表不涉及
ORIGINS = (ORIGIN_FILING, ORIGIN_DERIVED, ORIGIN_EXTERNAL, ORIGIN_JUDGMENT)


#: 比率的合理上界 —— 超过就当"取到的行不对"，不给参考值。
#: 有息债务成本超过 50%、有效税率超过 60%，都说明**取到的科目错行了**。
#: 给一个荒谬的数（还带"请确认"）比不给更坏：它看起来可用，会被直接抄进假设。
_MAX_DEBT_COST = 0.50
_MAX_TAX_RATE = 0.60


def _tax_reference(mat: Materials) -> tuple[str, str]:
    """所得税率的参考 —— 报表里"可能有"：有的年报直接给，有的要推。

    返回 `(参考文字, 来源类别)`。四种情况都照实说，**不编一个看起来合理的税率**：
      * 所得税费用与利润总额都在、且算出来合理 → 有效税率（**请确认**）
      * 利润总额为负 → 有效税率不适用（两个负数相除出来的比率没有意义）
      * 算出来超过 60% → 不合理，说明取到的行不对
      * 缺行 → 材料里没有所得税费用/利润总额
    """
    from financials.canonical import Field
    st = mat.statements
    inc = st.income.fields if (st and st.income) else {}
    tax = inc.get(Field.INCOME_TAX)
    pre = inc.get(Field.PRETAX_INCOME)
    if tax is None or pre is None:
        return "材料里没有所得税费用/利润总额 —— 请按法定税率填", ORIGIN_EXTERNAL
    if pre <= 0:
        return ("本期利润总额为负，有效税率不适用 —— 请按法定税率填",
                ORIGIN_EXTERNAL)
    rate = tax / pre
    if not (0 < rate <= _MAX_TAX_RATE):
        return (f"所得税费用 {tax:,.0f} ÷ 利润总额 {pre:,.0f} = {rate:.2%} —— "
                "这个税率**不合理**（多半取到了别的行），不给出有效税率；"
                "请按法定税率填", ORIGIN_EXTERNAL)
    return (f"有效税率 = 所得税费用 {tax:,.0f} ÷ 利润总额 {pre:,.0f} "
            f"= {rate:.2%}（**请确认**）", ORIGIN_DERIVED)


def _debt_cost_reference(mat: Materials) -> tuple[str, str]:
    """债务成本的参考 —— **多数利润表不单列利息费用**，所以常常是"没有数据"。

    有就按隐含利率给（利息费用 ÷ 有息负债，请确认）；没有就说没有，
    让用户填实际借款利率或 LPR+利差。**不拿行业平均利率顶上。**

    ## 荒谬的数不给（实测踩到）

    一份上市公司年报里「利息费用」有 `-74` 与 `214` 两个取值（合并表 / 母公司表），
    取到 `-74` 时算出来的债务成本是 **−0.41%**。负的债务成本是无意义的，
    但它挂着"请确认"的标签，看起来就像一个可以直接用的参考值 —— 最坏的那类错。
    """
    from financials.canonical import Field
    st = mat.statements
    inc = st.income.fields if (st and st.income) else {}
    bal = st.balance.fields if (st and st.balance) else {}
    ie = inc.get(Field.INTEREST_EXPENSE)
    debt = sum(v for v in (bal.get(Field.SHORT_TERM_DEBT),
                           bal.get(Field.LONG_TERM_DEBT)) if v)
    if ie is None:
        return ("材料里没有利息费用（报表未单列）—— "
                "请按实际借款利率或 LPR+利差填", ORIGIN_EXTERNAL)
    if not debt:
        return ("材料里没有有息负债（或为 0），算不出隐含利率 —— "
                "请按实际借款利率或 LPR+利差填", ORIGIN_EXTERNAL)
    rate = ie / debt
    if not (0 < rate <= _MAX_DEBT_COST):
        return (f"利息费用 {ie:,.0f} ÷ 有息负债 {debt:,.0f} = {rate:.2%} —— "
                "这个数**不合理**（利息费用为负、或取到了别的行），"
                "不给出隐含利率；请按实际借款利率或 LPR+利差填", ORIGIN_EXTERNAL)
    return (f"隐含利率 = 利息费用 {ie:,.0f} ÷ 有息负债 {debt:,.0f} "
            f"= {rate:.2%}（**请确认**）", ORIGIN_DERIVED)


def _debt_reference(mat: Materials) -> str:
    """有息负债的参考 —— 这是**财报里有的数**，工具必须给出来（不是让人去翻表）。"""
    from financials.canonical import Field
    st = mat.statements
    bal = st.balance.fields if (st and st.balance) else {}
    got = [(f.value, bal[f]) for f in (Field.SHORT_TERM_DEBT, Field.LONG_TERM_DEBT)
           if bal.get(f) is not None]
    if not got:
        return "材料里没有有息负债（本期无借款，或报表未单列）"
    return "财报：" + " ＋ ".join(f"{n} {v:,.0f}" for n, v in got)


def _choices(detected: str, *known: str) -> tuple[str, ...]:
    """下拉选项：**把材料里推出来的那个值放第一个**，后面跟常见取值。

    这样引擎认出来的口径永远选得中 —— 下拉框不会出现"当前值不在选项里、
    于是显示成空白"这种哑巴状态（哪怕它是个不常见的写法）。
    """
    d = (detected or "").strip()
    out = [d] if d and d not in known else []
    out += [k for k in known if k not in out]
    return tuple(out)


#: 这些键不填，对应的方法就整块不跑 —— 在报告里明说，不假装跑过了。
NEED = {
    "dcf": ("risk_free", "equity_risk_premium", "beta_unlevered", "cost_of_debt",
            "debt", "equity", "tax_rate", "growth", "ebitda_margin",
            "da_pct_revenue", "capex_pct_revenue", "nwc_pct_revenue",
            "terminal_growth"),
    "multiples": ("multiple_low", "multiple_mid", "multiple_high"),
}


def questions(mat: Materials, *, growth_years: int = 5) -> list[Q]:
    """列出这次估值必须由人给的输入。**不给默认值的键留空。**"""
    hist = {}
    if mat.statements is not None:
        try:
            hist = mat.statements.history()
        except Exception:                              # noqa: BLE001
            hist = {}

    def ref(name: str, pct: bool = True) -> str:
        v = hist.get(name)
        if v is None:
            return "数据不足"
        return f"{v:.2%}" if pct else f"{v:,.0f}"

    def cur(attr: str) -> str:
        """材料里推出来的当前值 —— **让用户能看见、能改**。"""
        return str(getattr(mat.statements, attr, "") or "") if mat.statements else ""

    rev = hist.get("历史实际收入")
    # 预测参数的参考值 —— **必须有"现在是多少"垫底**，否则等于让人凭空填。
    # 收入增长率推不出来（材料只给了一期，没有第二个年度可比）：
    # 那就是数据不足，说清楚，别拿"行业增速"之类的猜测顶上。
    growth_ref = (f"本期 {rev:,.0f} {mat.unit or ''} · 材料只有一期，"
                  "历史增长率推不出" if rev is not None else
                  "本期收入未取到；且材料只有一期，历史增长率推不出")
    # 折现率那两栏：**报表里可能有、也可能没有** —— 有就给数（标明"请确认"），
    # 没有就说没有，让人按法定税率 / LPR+利差 填。不编一个看起来合理的数。
    tax_ref, tax_origin = _tax_reference(mat)
    debt_cost_ref, debt_cost_origin = _debt_cost_reference(mat)
    debt_ref = _debt_reference(mat)

    # **默认值不能是「，，，，」**：那不是"空"，会被读成一个值，
    # 后面按年数对不上报错，用户看到的是个莫名其妙的提示。
    blank = ""
    # 货币按准则给个合理的默认：US GAAP 的报表一般是美元。
    # 给错了用户看得见（在问答清单里明写着），不给他就得自己想起来。
    cur_default = "USD" if "US GAAP" in (cur("gaap") or "") else "CNY"

    qs = [
        # ── 场景：六项不定，方法无从选 ──
        Q("target", "标的名称（报告封面上那个）", group="场景",
          default=mat.label),
        Q("unit", "金额单位（**错 1000 倍就是这里错**）", group="场景",
          default=mat.unit, hint="点选一个；清单里没有的单位可以自己写",
          suggest=("元", "千元", "万元", "百万元", "千美元", "百万美元")),
        Q("purpose", "估值目的", group="场景", default="并购定价",
          options=("并购定价", "融资定价", "投后NAV", "税务合规")),
        Q("stance", "立场", group="场景", default="买方",
          options=("买方", "卖方", "中立")),
        Q("stage", "发展阶段", group="场景", default="成熟企业",
          options=("成熟企业", "早期项目", "成长企业", "业主经营")),
        Q("valuation_date", "估值基准日", group="场景",
          default=cur("period") or str(getattr(mat.statements, "period", "") or ""),
          hint="多期数据按这个日期对齐", suggest=(cur("period"),) if cur("period") else ()),
        Q("currency", "货币", group="场景", default=cur_default,
          hint="点选一个，或自己写", suggest=("CNY", "USD", "HKD", "EUR", "SGD")),
        Q("equity_scope", "权益范围", group="场景", default="100%", unit="%"),

        # ── 口径：材料里推出来的，**可以覆盖**（下拉里第一个就是推出来的那个值）──
        Q("gaap", "会计准则（材料推出来的，不对就改）", group="口径",
          default=cur("gaap"), options=_choices(cur("gaap"), "CAS", "US GAAP", "IFRS")),
        Q("scope", "合并 or 单体（不对就改）", group="口径",
          default=cur("scope"), options=_choices(cur("scope"), "合并", "单体", "母公司报表")),
        Q("audited", "审计状态（材料推出来的，不对就改）", group="口径",
          default=cur("audited"), options=_choices(cur("audited"), "已审计", "未审计")),

        # ── 折现率：WACC 的每一项都要来源 ──
        # **公司之外的参数由人给，工具只写清该给什么**：
        # 无风险利率 / 股权风险溢价 / beta 来自市场，尽调材料里不会有。
        # 硬从报表凑一个，就是编数据。
        Q("risk_free", "无风险利率", group="折现率", unit="%",
          hint="对应货币的长期国债",
          reference="由你提供 —— 对应货币的长期国债收益率（公司之外，工具不提供）"),
        Q("equity_risk_premium", "股权风险溢价", group="折现率", unit="%",
          reference="由你提供 —— 成熟市场股权风险溢价 / 国别风险溢价（公司之外）"),
        Q("beta_unlevered", "去杠杆 beta", group="折现率",
          hint="上市可比公司的无杠杆 beta",
          reference="由你提供 —— 可比上市公司去杠杆 beta（第 4 步可比公司未接入）"),
        Q("cost_of_debt", "债务成本", group="折现率", unit="%",
          hint="实际借款利率或 LPR+利差",
          reference=debt_cost_ref, origin=debt_cost_origin),
        Q("tax_rate", "所得税率", group="折现率", unit="%",
          reference=tax_ref, origin=tax_origin),
        Q("debt", "有息负债（**市值口径**，WACC 用）", group="折现率",
          reference=debt_ref),
        Q("equity", "股权价值（市值口径，WACC 用）", group="折现率",
          reference="由你提供 —— 市值口径，材料里没有（未上市更没有）"),

        # ── 预测：这里是判断，不是算 ──
        Q("growth", f"{growth_years} 年收入增长率（逗号分隔）", group="预测",
          default=blank, hint="逐年给。只给一个数 = 全期沿用同一个", unit="%",
          reference=growth_ref),
        Q("ebitda_margin", f"{growth_years} 年 EBITDA 率（逗号分隔）", group="预测",
          reference=ref("历史 EBITDA 率"), unit="%"),
        Q("da_pct_revenue", "折旧摊销占收入比", group="预测",
          reference=ref("历史折旧摊销占收入比"), unit="%"),
        Q("capex_pct_revenue", "资本开支占收入比", group="预测",
          reference=ref("历史资本开支占收入比"), unit="%"),
        Q("nwc_pct_revenue", "净营运资本占收入比", group="预测",
          reference=ref("历史净营运资本占收入比"), unit="%"),
        Q("terminal_growth", "永续增长率", group="预测",
          hint="上界是长期名义 GDP 增速，引擎会拦", unit="%",
          reference="材料里推不出（需长期名义 GDP 增速，属外部数据）"),

        # ── 乘数法 ──
        Q("metric_name", "乘数用的指标", group="乘数法", default="EBITDA",
          options=("EBITDA", "EBIT", "收入", "SDE"),
          reference=metric_reference(mat, mat.unit)),
        Q("multiple_low", "倍数下沿", group="乘数法", hint="来自可比公司分位数"),
        Q("multiple_mid", "倍数中枢", group="乘数法"),
        Q("multiple_high", "倍数上沿", group="乘数法"),

        # ── 可选：不填就整块不跑，报告里会说 ──
        Q("sens_wacc", "敏感性网格 · WACC 各档（逗号分隔）", group="可选", unit="%"),
        Q("sens_growth", "敏感性网格 · 永续增长率各档（逗号分隔）", group="可选", unit="%"),
        Q("exit_multiple", "退出倍数交叉验证（如 12）", group="可选"),
        Q("ask_price", "对方要价（填了就出反向估值）", group="可选"),
        Q("advisor_tickers", "可比公司代码（逗号分隔，做假设参谋）", group="可选",
          hint="如 LEA, MGA, BWA —— 走公开域取 EDGAR 数据"),
        Q("advisor_market", "可比公司市场", group="可选", default="us",
          options=("us", "sh", "sz", "hk")),
        # 这三个就是引擎支持的全部（valuation/advisor.py 的 METRIC_FUNCS），
        # 有测试盯着它们一致 —— 免得下拉里给出引擎不认的指标。
        Q("advisor_metric", "参谋对照的指标", group="可选", default="revenue_cagr",
          options=("revenue_cagr", "ebitda_margin", "revenue_scale"),
          hint="引擎只支持这三项"),
    ]

    # 每个字段都要回答"这个数从哪来"。放在这里而不是散在每个 Q(...) 里，
    # 是为了让这张表**一眼能看完**：谁是报表里有的、谁是公司之外的、谁是你的判断。
    # `tax_rate` / `cost_of_debt` 已在上面按材料实际情况定过
    # （有数据=财报推算、没数据=外部），这里用 `or` 不覆盖它们。
    origins = {
        ORIGIN_FILING: ("target", "unit", "valuation_date", "currency",
                        "gaap", "scope", "audited", "debt"),
        ORIGIN_EXTERNAL: ("risk_free", "equity_risk_premium", "beta_unlevered",
                          "equity", "terminal_growth", "ask_price",
                          "advisor_tickers"),
        ORIGIN_JUDGMENT: ("purpose", "stance", "stage", "equity_scope", "growth",
                          "ebitda_margin", "da_pct_revenue", "capex_pct_revenue",
                          "nwc_pct_revenue", "metric_name", "multiple_low",
                          "multiple_mid", "multiple_high", "sens_wacc",
                          "sens_growth", "exit_multiple", "advisor_market",
                          "advisor_metric"),
    }
    for origin, keys in origins.items():
        for q in qs:
            if q.key in keys:
                q.origin = q.origin or origin
    return qs


def render_template(qs: list[Q], mat: Materials) -> str:
    """把问题排成一份**可填的 txt**。注释里带历史参考，值本身留空。"""
    out = [
        "# FreeAnalyst 估值问答清单",
        "#",
        "# 怎么用：把等号后面填上，然后跑",
        "#   python3 freeanalyst.py appraise <材料目录> --answers 这份文件",
        "#",
        "# 三条规矩：",
        "#   1  每个数后面可以跟来源，写成   key = 0.10  @管理层规划 p.12",
        "#       来源前加 高: / 中: / 低: 可指定置信度（默认中）；不写来源 = 低置信度",
        "#   2  注释里的「参考」是**历史值**，不是建议值。历史比率不等于预测假设，",
        "#       填多少是你的判断 —— 引擎不替你决定。",
        "#   3  空着的键不会替你猜：缺哪些，报告里就少哪一块，并会明说缺的是什么。",
        "#",
        mat.render().replace("\n", "\n# "),
        "#",
    ]
    group = None
    for q in qs:
        if q.group != group:
            group = q.group
            out.append("")
            out.append(f"[{group}]")
        out.append(q.line())
    out.append("")
    out.append("# 来源示例：@高:2016 年 10-K 审计报告")
    out.append("#          @管理层规划（CIM p.12）")
    out.append("#          不写 = 低置信度（会被列进「结果的软肋」）")
    return "\n".join(out) + "\n"


# ─────────────────────────── 读回答 ───────────────────────────

_KV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_CONF = {"高": "高", "中": "中", "低": "低"}


@dataclass
class Answer:
    value: str
    source: str = ""
    confidence: str = "低"          # 没写来源 = 低置信度


def parse_answer(text: str) -> Answer:
    """把「0.0245 @高:美国国债」这样一个值解析成 `Answer`。

    来源和置信度的规矩只有一条：**写了来源才算有来源。**
    没写来源的数字一律标成低置信度 —— 它会出现在报告的「结果的软肋」里，
    这是有意的：没有来源的假设就是低置信度的假设。
    """
    rest = text.strip()
    src, conf = "", "低"
    if "@" in rest:
        rest, tail = (x.strip() for x in rest.split("@", 1))
        src, conf = tail, "中"
        for c in _CONF:
            if tail.startswith(c + ":") or tail.startswith(c + "："):
                conf, src = c, tail[2:].strip()
                break
        src = src or "已注明（未写内容）"
    return Answer(value=rest, source=src, confidence=conf)


def read_answers(path: str | Path) -> dict[str, Answer]:
    """读问答清单。只认 `key = value`，`#` 开头的整行是注释。"""
    out: dict[str, Answer] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip() or line.strip().startswith("["):
            continue
        m = _KV.match(line.strip())
        if not m:
            continue
        key, rest = m.group(1), m.group(2).strip()
        # 只有标点的「值」等于没填 —— 模板里某些默认值会渲染成一串逗号，
        # 那不是值，读成值会在后面报一个「年数对不上」的莫名错误。
        if not rest or not rest.strip("，,、 "):
            continue
        out[key] = parse_answer(rest)
    return out


def _num(a: Answer) -> float | None:
    v = a.value.strip().rstrip("%")
    try:
        x = float(v)
    except ValueError:
        return None
    # 「11%」这种写法按百分数理解 —— 6 个人里 5 个会这么写。
    if a.value.strip().endswith("%"):
        return x / 100.0
    return x


def _lst(val: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，\s]+", val.strip()) if x.strip()]


def _num_text(x: str) -> float | None:
    """一个可能带 % 的字符串 → 数。**与 `_num` 同一条规则**（`8.5%` = 0.085）。

    列表字段（逐年增长率、敏感性各档）过去用的是裸 `float(x)`，
    于是 `10%` 这种写法直接把引擎崩掉、`10` 又静默变成 1000%。
    「带 % 就按百分数」这条规则必须**处处一致** —— 只在一个地方生效，
    就等于把不一致留给了另一个入口。
    """
    return _num(Answer(value=x))


def _nums_of(text: str) -> list[float | None]:
    """逗号/空格分隔的一串值 → 一串数（认不出的位置是 None，**不猜**）。"""
    return [_num_text(x) for x in _lst(text)]


#: 「看起来就是一个数字」——只补这种，别把 `数据不足`、`待定` 变成 `待定%`。
_PLAIN_NUM = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)$")


def as_percent_text(raw: str) -> str:
    """把「人只填了数值」的百分数补成带 % 的写法 —— **每个逗号分隔的元素都补**。

    网页向导里百分比字段把 `%` 显示在框里，人只填数值（填 `8.5` 就是 8.5%）。
    但引擎拿到的字符串**必须自带单位**：光一个 `8.5` 会被读成 850% ——
    那是 100 倍级的静默错误，正是这个项目最怕的一类错。

    规则：
      * 只补「看起来是纯数字」的元素：`8.5` → `8.5%`；`10,9,8` → `10%,9%,8%`
      * 已经带 % 的不重复补：`8.5%` 原样
      * 补在 `@来源` 之前：`8.5 @管理层 p.12` → `8.5% @管理层 p.12`
      * 非数字（`数据不足`）原样放过 —— 交给下游判成缺失，不在这里编造
    """
    s = (raw or "").strip()
    if not s:
        return s
    head, at, tail = s.partition("@")
    items = [x for x in re.split(r"[,，\s]+", head.strip()) if x]
    if not items:
        return s
    out = [x if x.endswith("%") or not _PLAIN_NUM.match(x) else f"{x}%" for x in items]
    body = ",".join(out)
    return f"{body} @{tail}" if at else body


def percent_keys(mat: Materials, *, growth_years: int = 5) -> set[str]:
    """哪些键在界面上是以百分数显示的（`Q.unit == "%"`）。"""
    return {q.key for q in questions(mat, growth_years=growth_years) if q.unit == "%"}


def normalize_percents(mat: Materials, ans: dict[str, Answer], *,
                       growth_years: int = 5) -> list[str]:
    """把**网页收上来的**百分数补上 %（就地改 `ans`），返回被补过的键。

    **只走网页这条路。** txt 清单那条路保持小数写法：那份清单里人写的是
    `0.085`（= 8.5%），若在那边也补 % 就变成 0.085%（差 100 倍）——
    同一条规则在两个入口会产生相反的错误，所以规则只加在显示着 % 的那一边。
    """
    fixed: list[str] = []
    for k in sorted(percent_keys(mat, growth_years=growth_years)):
        a = ans.get(k)
        if a is None or not (a.value or "").strip():
            continue
        new = as_percent_text(a.value)
        if new != a.value:
            a.value = new
            fixed.append(k)
    return fixed


def _spec(a: Answer, *, unit: str = "") -> dict:
    """一个假设 → `value.py` 认的对象形式（带来源与置信度）。"""
    d: dict = {"value": _num(a), "source": a.source or "未注明（裸数字）",
               "confidence": a.confidence}
    if unit:
        d["unit"] = unit
    return d


def _specs_of(values: list[str], a: Answer, unit: str = "") -> list[dict]:
    """一组值 → 一组假设。**来源继承整条回答**（一个键一个来源）。"""
    return [_spec(Answer(value=x, source=a.source, confidence=a.confidence), unit=unit)
            for x in values]


def _specs(a: Answer, unit: str = "") -> list[dict]:
    return _specs_of(_lst(a.value), a, unit)


def build_config(mat: Materials, ans: dict[str, Answer],
                 *, growth_years: int = 5) -> tuple[dict, list[str]]:
    """把材料 + 回答拼成一份 `value.py` 认的配置。

    返回 `(cfg, 缺什么)` —— 缺的项对应的方法**整块不生成**，
    并在报告里明说是缺什么才没跑。
    """
    def has(k: str) -> bool:
        return k in ans and ans[k].value.strip() != ""

    def raw(k: str, default=None):
        return ans[k].value if has(k) else default

    def require(keys, label: str) -> list[str]:
        """挑出还没给的必需项。

        **返回的是键名列表，不是提示文字** —— 之前这里比字符串
        （`k in missing`）判断「缺不缺」，判断永远为假，于是缺一项也会
        整块生成，最后在取回答时 `KeyError` 崩掉。缺就从结构上拿住。
        """
        miss = [k for k in keys if not has(k)]
        for k in miss:
            missing.append(f"{label} · {k}")
        return miss

    unit = raw("unit", mat.unit) or ""
    missing: list[str] = []
    cfg: dict = {
        "target": raw("target", mat.label),
        "unit": unit,
        "scenario": {
            "purpose": raw("purpose", "并购定价"),
            "stance": raw("stance", "买方"),
            "stage": raw("stage", "成熟企业"),
            "valuation_date": raw("valuation_date", ""),
            "currency": raw("currency", "CNY"),
            "equity_scope": raw("equity_scope", "100%"),
        },
        # 材料来源留档（**不进 statements 节**：PDF/Excel 没法只靠配置复现，
        # 复现的正确方式是重跑 appraise）
        "materials": {
            "dir": str(mat.directory),
            "source": mat.source,
            "unit": unit,
            "gaap": getattr(mat.statements, "gaap", ""),
            "scope": getattr(mat.statements, "scope", ""),
            "period": getattr(mat.statements, "period", ""),
            "detected": mat.detected,
        },
    }

    # ---------- 折现率（DCF 的必需项） ----------
    wacc_keys = ("risk_free", "equity_risk_premium", "beta_unlevered",
                 "cost_of_debt", "debt", "equity", "tax_rate")
    if not require(wacc_keys, "折现率"):
        cfg["wacc"] = {
            "risk_free": _spec(ans["risk_free"]),
            "equity_risk_premium": _spec(ans["equity_risk_premium"]),
            "beta_unlevered": _spec(ans["beta_unlevered"]),
            "tax_rate": _spec(ans["tax_rate"]),
            "cost_of_debt": _spec(ans["cost_of_debt"]),
            "debt": _spec(ans["debt"], unit=unit),
            "equity": _spec(ans["equity"], unit=unit),
        }
        for opt in ("size_premium", "country_risk_premium"):
            if has(opt):
                cfg["wacc"][opt] = _spec(ans[opt])

    # ---------- DCF ----------
    dcf_need = ("growth", "ebitda_margin", "da_pct_revenue",
                "capex_pct_revenue", "nwc_pct_revenue", "terminal_growth")
    dcf_miss = require(dcf_need, "预测")

    # 年数与逐年取值先在**一处**算清：dcf.py 要求 years/revenue/ebitda_margin
    # 三者长度一致，对不上就整块不跑（而不是猜着补齐）。
    gy = _lst(ans["growth"].value) if has("growth") else []
    years = len(gy)
    gm = _lst(ans["ebitda_margin"].value) if has("ebitda_margin") else []
    if len(gm) == 1 and years > 1:
        gm = gm * years

    dcf_ready = ("wacc" in cfg and not dcf_miss
                 and "base_revenue" in _facts_keys(mat))
    if dcf_ready and (not years or len(gm) != years):
        missing.append(f"预测 · ebitda_margin 给了 {len(gm)} 个，"
                       f"增长率给了 {years} 个（年数对不上）")
        dcf_ready = False
    # 逐年增长率：**逐项走 `_num` 的规则**（`8.5%` 与 `0.085` 都认）。
    # 以前这里是裸 `float(x)` —— 同一个「带 % 就按百分数」的规则，
    # 只在一个地方生效就等于把不一致留给另一个入口（实测 `10%` 会崩、`10` 会变 1000%）。
    growth_vals = _nums_of(ans["growth"].value) if has("growth") else []
    if growth_vals and any(g is None for g in growth_vals):
        missing.append("预测 · growth（有认不出的数：写成 8.5% 或 0.085 都可以）")
        dcf_ready = False
    if dcf_ready:
        base = _facts_number(mat, "base_revenue")
        growth = [g for g in growth_vals if g is not None]   # 上面已拦过认不出的数
        gsrc = ans["growth"].source or "未注明"
        revs, r = [], float(base or 0.0)
        for g in growth:
            r *= (1 + g)
            revs.append({"value": r, "unit": unit, "confidence": ans["growth"].confidence,
                         "source": f"由基期收入 {base:,.0f} 与逐年增长率推算"
                                   f"（增长率来源：{gsrc}）"})
        # `years` 是**年份标签列表**，不是年数 —— `dcf.py` 拿它 len()，
        # 报告里也按年份展示。从估值基准日推；推不出就先用序号占位并说出来。
        ym = _YEAR.search(raw("valuation_date", "") or "")
        if ym:
            y0 = int(ym.group(0))
            labels = list(range(y0 + 1, y0 + 1 + years))
        else:
            labels = list(range(1, years + 1))
            missing.append("预测年份标签（估值基准日里没有年份，先用 1…N 占位）")
        cfg["dcf"] = {
            "years": labels,
            "base_revenue": _spec(Answer(value=f"{base}", source="三张表（最近一个实际年度）",
                                         confidence="高"), unit=unit),
            "revenue": revs,
            # **用展开后的 `gm`，不是原样再解析一遍** —— 用户只给一个
            # EBITDA 率时会被展开到每一年，这里若重新 `_lst` 就又变回一个，
            # dcf.py 会因长度不一致直接报错。
            "ebitda_margin": _specs_of(gm, ans["ebitda_margin"], unit),
            "tax_rate": _spec(ans["tax_rate"]),
            "da_pct_revenue": _spec(ans["da_pct_revenue"]),
            "capex_pct_revenue": _spec(ans["capex_pct_revenue"]),
            "nwc_pct_revenue": _spec(ans["nwc_pct_revenue"]),
            "terminal_growth": _spec(ans["terminal_growth"]),
        }
        if has("exit_multiple"):
            em = _num_text(ans["exit_multiple"].value)
            if em is None:
                missing.append("可选 · exit_multiple（认不出这个数，未采用）")
            else:
                cfg["dcf"]["exit_multiple"] = em
        if has("sens_wacc") and has("sens_growth"):
            sw = _nums_of(ans["sens_wacc"].value)
            sg = _nums_of(ans["sens_growth"].value)
            if any(x is None for x in sw + sg):
                missing.append("可选 · 敏感性网格（有认不出的数，整块未生成）")
            else:
                cfg["dcf"]["sensitivity"] = {"wacc": sw, "growth": sg}
        else:
            missing.append("可选 · 敏感性网格（sens_wacc / sens_growth）"
                           "—— 没有它 Football Field 的区间只能取点值")

    # ---------- 乘数法 ----------
    mult_miss = require(("multiple_low", "multiple_mid", "multiple_high"), "乘数法")
    metric = (raw("metric_name", "EBITDA") or "EBITDA").strip()
    # 下拉里的每个指标都要真的接通：以前只有 EBITDA 与收入接了，
    # 选 EBIT 会得到一句"材料里推不出这个指标" —— 而下拉里明明有它。
    mkey = METRIC_KEYS.get(metric, "ebitda")
    mv = _facts_number(mat, mkey)
    if mv is None:
        missing.append(f"乘数法 · 指标值（{metric}）—— "
                       f"{_METRIC_WHY.get(mkey, '材料里推不出')}；"
                       "要么在材料里补，要么把 metric_name 换成别的")
    if not mult_miss and mv is not None:
        cfg["multiples"] = {
            "metric_name": metric,
            "metric_value": {"value": mv, "unit": unit, "source": "三张表推算",
                             "confidence": "高"},
            "multiple_low": _spec(ans["multiple_low"]),
            "multiple_mid": _spec(ans["multiple_mid"]),
            "multiple_high": _spec(ans["multiple_high"]),
        }

    # ---------- 反向估值 ----------
    if has("ask_price"):
        ap = _num_text(ans["ask_price"].value)
        if ap is None:
            missing.append("可选 · ask_price（认不出这个数，未做反向估值）")
        else:
            cfg["ask_price"] = ap

    # ---------- 假设参谋（可选，走公开域） ----------
    if has("advisor_tickers"):
        tickers = _lst(ans["advisor_tickers"].value)
        cfg["advisor"] = {"peer_sets": {
            f"可比公司（{raw('advisor_market', 'us')}）": {
                "tickers": tickers,
                "metric": raw("advisor_metric", "revenue_cagr"),
                "years": growth_years,
                "as_of": cfg["scenario"]["valuation_date"] or "",
            }}}
    return cfg, missing


#: 「乘数用的指标」四个选项 → 从材料里取哪个数（`_facts_number` 的键）。
#: **下拉里给的每个指标都必须真的接通** —— 给引擎不认的选项就是骗人。
METRIC_KEYS: dict[str, str] = {
    "EBITDA": "ebitda",
    "EBIT": "ebit",
    "收入": "base_revenue",
    "SDE": "sde",
}

#: 取不到时**为什么**（照实说，不省略 —— 省略会让人以为那个选项也能用）。
_METRIC_WHY: dict[str, str] = {
    "ebitda": "缺折旧摊销（通常在现金流量表间接法段或附注）",
    "ebit": "缺营业利润",
    "base_revenue": "缺营业收入",
    "sde": "需所有者薪酬与一次性项目，材料里没有",
}


def metric_reference(mat: Materials, unit: str = "") -> str:
    """「乘数用的指标」那一栏的参考：**四个指标各自的本期数值**。

    选之前先看见基数 —— 否则 EBITDA / EBIT / 收入 / SDE 只是四个词，
    选完才知道引擎拿哪个数去乘倍数。
    """
    parts = []
    for name, key in METRIC_KEYS.items():
        v = _facts_number(mat, key)
        if v is None:
            parts.append(f"{name} 推不出（{_METRIC_WHY[key]}）")
        else:
            parts.append(f"{name} {v:,.0f}")
    tail = f"（{unit}）" if unit else ""
    return "本期：" + " ／ ".join(parts) + tail


def _facts_keys(mat: Materials) -> dict:
    if mat.statements is None:
        return {}
    try:
        return mat.statements.facts()
    except Exception:                                  # noqa: BLE001
        return {}


def _facts_number(mat: Materials, key: str) -> float | None:
    if key == "sde":
        # SDE = EBITDA + 所有者薪酬调整 + 一次性项目 —— 后两项材料里没有，
        # 硬算会得到一个"看起来对"的数（还带着一个来源标签，最坏的那种错）。
        # 所以这里**永远返回 None**，由 metric_reference 说明为什么。
        return None
    if key == "ebit":
        st = mat.statements
        if st is None or st.income is None:
            return None
        from financials.canonical import Field
        # 营业利润就是 EBIT 的口径（不再减利息/税：那两项在它下面）。
        return st.income.fields.get(Field.OPERATING_INCOME)
    if key == "ebitda":
        st = mat.statements
        if st is None:
            return None
        inc = getattr(st, "income", None)
        if inc is None:
            return None
        from financials import derive as dv
        # **三张表合起来取数**：折旧摊销在现金流量表的间接法段里，不在利润表。
        # 只喂利润表的话 EBITDA 会是 None（乘数法跟着整块空掉）—— 实测 Fitbit
        # 这类 10-K 正是这样。取数口径与 history() 同一个（`da_total()`）。
        fields = dict(inc.fields)
        cf = getattr(st, "cash_flow", None)
        if cf is not None:
            fields.update(cf.fields)
        da = st.da_total()
        if da is not None:
            fields.setdefault(dv.Field.DEPRECIATION_AMORTIZATION, da)
        try:
            d = dv.ebitda(fields)
        except Exception:                              # noqa: BLE001
            return None
        return getattr(d, "value", None)
    f = _facts_keys(mat).get(key)
    return getattr(f, "value", None)


# ─────────────────────────── 编排 ───────────────────────────

#: 项目根目录 —— 默认输出落在它下面的 `out/`（已在 .gitignore 里）。
ROOT = Path(__file__).resolve().parent


def _slug(text: str) -> str:
    """把标的/材料名变成一个能当目录名的短串。"""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(text)).strip("_")
    return s or "估值"


def out_dir_for(mat: Materials, override: str | Path | None = None) -> Path:
    """这次运行的产物目录。

    ## 默认不放在材料旁边（实测踩到）

    第一版把「估值问答.txt」写在**材料所在目录**里 —— 一跑就把文件丢进了
    用户的私有材料库（实测往三个真实项目的目录里各扔了一份）。
    **工具不该往用户的材料库里写东西**，那条库是只读的输入。

    默认改成 `<项目根>/out/<标的>/`：一个标的一个目录，配置、问答清单、
    报告都在里面，而 `out/` 本来就是这个项目放"本地生成的报告产物"的地方
    （也已经在 .gitignore 里）。
    """
    if override:
        return Path(override)
    return ROOT / "out" / _slug(mat.label)


def appraise(directory: str | Path, answers: str | Path | None = None,
             *, unit: str = "", out_dir: str | Path | None = None,
             no_trace: bool = False, growth_years: int = 5) -> int:
    """材料目录 → 报告。没给 `answers` 时先出问答清单。"""
    d = Path(directory).expanduser().resolve()
    try:
        mat = scan(d, unit=unit)
    except (NotADirectoryError, FileNotFoundError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    print("─" * 74)
    print("材料扫描")
    print("─" * 74)
    print(mat.render())
    print()

    if mat.statements is None:
        print("✗ 三张表一张都没认出来 —— 先解决这个，后面的估值没有基础。")
        print("  常见的三种原因：① 材料是扫描件且 OCR 没跑（仅 macOS 支持）")
        print("  ② 这份材料本来就不是三大报表（比如是商业计划书）")
        print("  ③ 文件是我没见过的格式")
        return 1

    # 勾稽先跑一遍，让「能不能用这份材料」当场有答案。
    try:
        checks = mat.statements.checks()
        bad = [c for c in checks if c.ok is False]
        unknown = [c for c in checks if c.ok is None and c.applicable]
        if bad:
            print(f"⚠ 勾稽不平 {len(bad)} 条 —— 下面的推算结果先别用，"
                  "先看是哪一行归属错了")
            # 把最可能的原因直接指出来。实测一份 A 股审计报告：**同一页上
            # 既有合并表又有母公司表**，OCR 抽出来的科目出现多个取值，
            # 取值取串了 → 勾稽不平。这条提示让"不平"变成可动手的事，
            # 而不是让人对着一个差额发呆。
            dups = [w for w in mat.statements.warnings
                    if "个取值" in w or "出现 2 个" in w]
            if dups:
                print(f"  这份材料有 {len(dups)} 个科目**出现多个取值** —— "
                      "很可能是同一页上既有合并表又有母公司表，取值取串了。")
                print("  先确认取的是合并那一列（合并表通常排在母公司表前面）。")
        elif unknown:
            print(f"⚠ 有 {len(unknown)} 条勾稽判不了（缺科目），"
                  "那些口径按缺口处理")
        else:
            print("✓ 勾稽都能判、而且都平")
        print()
    except Exception as exc:                           # noqa: BLE001
        print(f"⚠ 勾稽校验本身出错了（{type(exc).__name__}: {exc}）—— 照实报出来")
        print()

    qs = questions(mat, growth_years=growth_years)
    # 产物统一放 `out/<标的>/`，**不往材料目录里写**（材料库是只读输入）。
    out = out_dir_for(mat, out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tpl = out / "估值问答.txt"
    tpl.write_text(render_template(qs, mat), encoding="utf-8")

    if answers is None:
        print(f"问答清单已写好：{tpl}")
        print("填完再跑一次（加 --answers 指向它），中间不用碰任何 JSON。")
        print()
        print("这次必须你给的（空着的都不会替你猜）：")
        for q in qs[:12]:
            if not q.default:
                print(f"  · {q.label}")
        return 0

    ans = read_answers(answers)
    print(f"读到回答 {len(ans)} 条（{Path(answers).name}）")
    if not (ans.get("unit") or mat.unit):
        print("✗ 单位还没确定（报表可能是千美元，引擎默认万元 —— 差 1000 倍）。")
        print("  在问答清单里把 unit 填上，或加 --unit 千美元 重跑。")
        return 1

    cfg, missing = build_config(mat, ans, growth_years=growth_years)

    # 口径三项：问答清单里能覆盖材料推出来的值 ——
    # **机器只推，改不改由人定。** 推错的口径无声无息，所以留一个改的入口。
    for attr in ("gaap", "scope", "audited"):
        if ans.get(attr) and ans[attr].value.strip():
            setattr(mat.statements, attr, ans[attr].value.strip())

    from value import run_report

    stem = _slug(cfg.get("target") or "估值")
    cfg_path = out / f"{stem}.config.json"
    report_path = out / f"{stem}.报告.txt"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    report = run_report(cfg, cfg_path.parent, statements=mat.statements,
                        no_trace=no_trace)

    head = ["=" * 74, "本次没跑的与没给的（**不是没发生**）", "=" * 74]
    if missing:
        for m in dict.fromkeys(missing):
            head.append(f"  ✗ {m}")
        head.append("")
        head.append("  上面每一项都对应报告里少掉的一块。缺不是错，"
                    "**错了的是不给却说跑过了**。")
    else:
        head.append("  无 —— 该给的全给了")
    text = "\n".join(head) + "\n\n" + report
    report_path.write_text(text, encoding="utf-8")

    print()
    print(text)
    print()
    print(f"配置留档：{cfg_path}")
    print(f"报告落盘：{report_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="材料目录 → 估值报告")
    ap.add_argument("materials", help="材料目录")
    ap.add_argument("--answers", help="填好的问答清单")
    ap.add_argument("--unit", default="", help="金额单位（认不出时用这个声明）")
    ap.add_argument("--out", help="配置与报告的输出目录（默认为材料目录）")
    ap.add_argument("--no-trace", action="store_true", help="不打印计算追溯")
    ap.add_argument("--years", type=int, default=5, help="预测年数（默认 5）")
    args = ap.parse_args()
    return appraise(args.materials, args.answers, unit=args.unit, out_dir=args.out,
                    no_trace=args.no_trace, growth_years=args.years)


if __name__ == "__main__":
    sys.exit(main())
