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
from pathlib import Path

from ingest import excel
from ingest.html import _to_number

from . import canonical as cn
from . import statements as stm

#: 各表独有的科目名 —— 用来判断一个 sheet 是哪张表
_MARKERS = {
    "balance": ("资产总计", "负债合计", "负债及所有者权益总计", "流动资产合计",
                "资产负债表", "所有者权益合计"),
    "income": ("净利润", "利润总额", "营业利润", "营业收入", "产品销售收入",
               "损益表", "利润表", "产品销售利润"),
    "cash_flow": ("经营活动", "现金流量表", "投资活动", "筹资活动",
                  "期末现金及现金等价物余额"),
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


def load_excel_statements(paths: list[str | Path],
                          unit: str = "") -> stm.Statements:
    """把若干 Excel 文件读成一套 `Statements`。

    一个文件里可能有左右两栏（资产负债表），会被合并成一套。
    """
    S = stm.Statements(gaap="CAS", scope="单体", audited="未标注")
    warnings: list[str] = []

    for p in paths:
        p = Path(p)
        wb = excel.read_workbook(p)
        for sh in wb.sheets:
            kind = classify(sh.rows)
            if kind == "unknown":
                warnings.append(f"{p.name} 的 sheet「{sh.name}」认不出是哪张表，跳过")
                continue

            if kind == "balance":
                target = S.balance or stm.StatementSet(
                    name="资产负债表", source=p.name, unit=unit)
                S.balance = target
            elif kind == "income":
                target = S.income or stm.StatementSet(
                    name="利润表", source=p.name, unit=unit)
                S.income = target
            else:
                target = S.cash_flow or stm.StatementSet(
                    name="现金流量表", source=p.name, unit=unit)
                S.cash_flow = target

            if not S.period:
                _, per = _meta(sh.rows)
                if per:
                    S.period = per

            # 左右两栏各自成一个块，都灌进同一张表
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
