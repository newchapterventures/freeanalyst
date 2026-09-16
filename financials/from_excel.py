"""从 Excel 报表装载三张表。

## 用法

    from financials.from_excel import load_excel_statements
    S = load_excel_statements(["资产负债表2024.xls", "利润表2024.xls"])

## 怎么判断哪个文件是哪张表

**不看文件名**，看内容 —— 文件名太不可靠（`1财务报表` / `第12页` 这种）。

判据是各表**独有**的科目名：出现「资产总计」的是资产负债表，
出现「净利润」的是损益表，出现「经营活动」的是现金流量表。

## 为什么要把左右两栏合并成一张表

小企业的资产负债表是 T 型布局：左边资产、右边负债及所有者权益，
同一行里装着两边的数。`ingest/excel.py` 会按列切开成两块，
这里再把两块合并回一个 `StatementSet`。

**年度口径必须显式记下来。** 资产负债表的列是「年初数 / 期末数」，
损益表是「本月数 / 本年累计」—— 两个「另一列」的含义完全不同，
混为一谈会让用户以为在比同一件事。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ingest import excel
from ingest.html import _to_number

from . import canonical as cn
from . import statements as stm

#: 各表独有的科目名 —— 用来判断一个 sheet 是哪张表
#:
#: **中英双语。** 只写中文的时候，美国公司的英文报表一份都认不出来 ——
#: 而且 `classify` 会给所有 sheet 打 0 分、全部判成 unknown，
#: 结果是**整份材料静默返回空**（实测 MBA Mentored Study 那两份）。
_MARKERS = {
    "balance": ("资产总计", "负债合计", "负债及所有者权益总计", "流动资产合计",
                "资产负债表", "所有者权益合计",
                "Total Assets", "Total Liabilities", "Current Assets",
                "Total Current Assets", "Total Current Liabilities",
                "Retained Earnings", "Balance Sheet", "Total Equity",
                "Stockholders Equity", "Shareholders Equity"),
    "income": ("净利润", "利润总额", "营业利润", "营业收入", "产品销售收入",
               "损益表", "利润表", "产品销售利润",
               "Net Income", "Total Revenues", "Total Revenue", "Gross Profit",
               "Operating Income", "Cost of Sales", "Income Statement",
               "Cost of Revenue", "Income Before Taxes"),
    "cash_flow": ("经营活动", "现金流量表", "投资活动", "筹资活动",
                  "期末现金及现金等价物余额",
                  "Cash Flows", "Operating Activities", "Investing Activities",
                  "Financing Activities", "Net Cash", "Cash Flow Statement"),
}

#: `编制单位:江苏新锐环境监测有限公司` / `2024 年12 月 31 日`
_ENTITY = re.compile(r"编\s*制\s*单\s*位\s*[:：]\s*(.+)")
_PERIOD = re.compile(r"(20\d{2})\s*年")


def classify(rows: list[list[object]]) -> str:
    """这个 sheet 是哪张表。"""
    text = " ".join(excel._cell_str(v) for r in rows for v in r)
    scores = {k: sum(1 for m in ms if m in text) for k, ms in _MARKERS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else "unknown"


def _meta(rows: list[list[object]]) -> tuple[str, str]:
    """从表头抓「编制单位」和年份。"""
    text = " ".join(excel._cell_str(v) for r in rows[:6] for v in r)
    ent = _ENTITY.search(text)
    per = _PERIOD.search(text)
    return (ent.group(1).strip() if ent else "",
            f"{per.group(1)}-12-31" if per else "")


def _into(set_: stm.StatementSet, rows_out: list[list[str]]) -> None:
    for lab, note, primary, other in rows_out:
        f, _ = cn.identify(lab)
        if not f:
            continue
        v = _to_number(primary) if primary else None
        if v is not None and f not in set_.fields:
            set_.fields[f] = v
        set_.rows.append(stm.StatementRow(label=lab, value=v, field=f, via="excel"))


#: 表名里出现这些词的，是「合并/汇总」视图，优先选
_CONSOLIDATED_HINT = ("summary", "consolidated", "annual", "total", "company",
                      "combined", "overall", "group")
#: 表名里出现这些词的，是「某分部」或「月度」视图，降级
_UNIT_HINT = ("month", "monthly", "weekly", "daily", "detail")
_YEARISH = re.compile(r"^(19|20)\d{2}$")


def _period_columns(rows: list[list[object]]) -> int:
    """表头里有几个「年度 / 日期」列。

    真表的列是**时间**（2015 / 2016-12-31）；分部表的列是**地名**，
    月度表的列是 Jan/Feb。这一条把后者排除掉。
    """
    best = 0
    for r in rows[:8]:
        n = 0
        for v in r:
            if isinstance(v, datetime):
                n += 1
                continue
            s = excel._cell_str(v).strip()
            if _YEARISH.match(s) or re.match(r"^\d{4}-\d{2}-\d{2}", s):
                n += 1
        best = max(best, n)
    return best


def _sheet_score(name: str, rows: list[list[object]]) -> int:
    """这张 sheet 有多像「该公司的正表」。

    ## 为什么要打分（实测：Cicero 那份利润表有 12 张 sheet 都判成 income）

    `Income Statement Summary.xlsx` 里每一张 sheet 都含「Net Income」，
    所以全都判成 income：

        「2015」「2016」「2017」「2018」   列 = 分部（Boulder/Alameda/…）
        「Annual Summary」                列 = 年度   ← 这才是合并视图
        「Boulder」「Alameda」…           列 = 年度，但只有一个分部
        「Revenues」                     只有收入的分解

    拿第一张（「2015」）会拿到**分部拆解**而不是**公司的利润表**，
    列的含义完全不同 —— 而 `describe()` 会把它当成期间列报出来。

    打分三块：映射上的科目数 + 表名像不像汇总 + 有没有期间列。
    """
    mapped = 0
    for r in rows:
        for v in r:
            s = excel._cell_str(v).strip()
            if s and len(s) < 48 and cn.identify(s)[0]:
                mapped += 1
    low = name.lower()
    score = mapped
    if any(h in low for h in _CONSOLIDATED_HINT):
        score += 500
    if any(h in low for h in _UNIT_HINT):
        score -= 300
    score += 200 * min(_period_columns(rows), 3)
    return score


def load_excel_statements(paths: list[str | Path],
                          unit: str = "") -> stm.Statements:
    """把若干 Excel 文件读成一套 `Statements`。

    一个文件里可能有左右两栏（资产负债表），会被合并成一套。

    一个工作簿里可能有多张同类 sheet（分部表 / 月度表 / 年度表），
    **按 `_sheet_score` 选最像正表的那张**，其余的记进 `warnings` 里说明。
    """
    if isinstance(paths, (str, Path)):
        # 传单个路径是常见误用 —— 会变成逐字符遍历，报一个看不懂的后缀错误
        raise TypeError("paths 要传**列表**，比如 load_excel_statements([\"a.xls\"])")

    S = stm.Statements(gaap="", scope="", audited="未标注")
    warnings: list[str] = []
    header_texts: list[str] = []

    # 先扫一遍，按类型收集候选
    cands: dict[str, list[tuple[int, Path, object, str]]] = {
        "balance": [], "income": [], "cash_flow": []}
    for p in paths:
        p = Path(p)
        wb = excel.read_workbook(p)
        for sh in wb.sheets:
            kind = classify(sh.rows)
            if kind == "unknown":
                continue
            cands[kind].append((_sheet_score(sh.name, sh.rows), p, sh, sh.name))

    for kind, label in (("balance", "资产负债表"), ("income", "利润表"),
                        ("cash_flow", "现金流量表")):
        pool = cands[kind]
        if not pool:
            continue
        pool.sort(key=lambda x: -x[0])
        _, p, sh, best_name = pool[0]
        target = stm.StatementSet(name=label, source=p.name, unit=unit)
        setattr(S, kind, target)

        if len(pool) > 1:
            others = "、".join(nm.strip() for _, _, _, nm in pool[1:6])
            warnings.append(
                f"{label}：这个工作簿里有 {len(pool)} 张同类表，选了「{best_name.strip()}」"
                f"（分最高）；其余未用：{others}")

        if not S.period:
            _, per = _meta(sh.rows)
            if per:
                S.period = per

        for block in excel.split_blocks(sh.rows):
            out, roles = excel.to_rows(block)
            if not roles.ok:
                warnings.append(f"{p.name}／{sh.name}：读不出列名，跳过一块")
                continue
            if roles.primary_name and roles.primary_name not in target.columns:
                target.columns.append(roles.primary_name)
            if roles.other_name and roles.other_name not in target.columns:
                target.columns.append(roles.other_name)
            _into(target, out)

        # **表头文字也要进判据** —— 「会工01表」这种表号在标题行里，
        # 不在科目列里。只拿科目名判的话，新锐环境那种真正的 1993 年
        # 格式会被判成普通 CAS（实测）。
        header_texts.append(" ".join(
            excel._cell_str(v) for r in sh.rows[:8] for v in r))

    # 口径从**内容**推断，不写死（见 `meta.py`）。
    # 写死的时候：美国公司的报表被报成 CAS，非上市单体审计报告被报成合并。
    #
    # **把三张表的标签并起来判一次**，不要一张一张判 —— 第一版逐表判，
    # 利润表的特征词命不中就地返回「未判定」，资产负债表的信息被丢掉了。
    from . import meta

    text = " ".join(
        [r.label for st in (S.balance, S.income, S.cash_flow) if st
         for r in st.rows] + header_texts)
    if not S.gaap:
        S.gaap = meta.detect_gaap(text)
    if not S.scope:
        S.scope = meta.detect_scope(text)

    S.warnings = warnings
    return S


def describe(S: stm.Statements) -> str:
    """把装载结果写成一段人看的说明。**列的含义要说清楚。**"""
    lines = []
    for label, st in (("资产负债表", S.balance), ("利润表", S.income),
                      ("现金流量表", S.cash_flow)):
        if st is None:
            lines.append(f"  {label}：未提供")
            continue
        mapped = sum(1 for r in st.rows if r.field)
        cols = "、".join(st.columns) if st.columns else "（未识别）"
        lines.append(f"  {label}：{len(st.rows)} 行，映射上 {mapped} 行")
        lines.append(f"      本表列：{cols}")
        if st.columns:
            lines.append(f"      ↑ **主数值取「{st.columns[0]}」**，"
                         f"不是「{st.columns[1] if len(st.columns) > 1 else '—'}」")
    if S.warnings:
        lines.append("  ⚠ " + "；".join(S.warnings))
    return "\n".join(lines)
