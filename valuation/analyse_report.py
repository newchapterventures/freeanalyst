"""财务分析报告 —— 把杜邦结果落成一份能存档、能转发的文本。

## 为什么它跟估值报告不是一回事 (2026-09-30)
估值报告里有假设、折现率、区间 —— 那些都是**人给的**。
财务分析只读三张表：**没有任何假设**，所以它本身就是完整的一份，
不需要先回答那 36 条问题。

这也是两条路能分开的原因：想做财务分析的人，不该先回答"永续增长率是多少"。

## 报告里不写"工具算的"，写"取自哪张表、哪一列"
三个数（总资产 / 净利 / 权益）都有原始科目名和数值列名可查 ——
出处可追溯是硬标准，写不出来就写"没定位到"，**不装作有**。
"""
from __future__ import annotations

from financials.canonical import Field

#: 报告要交代来源的几个数（与 `dupont` 实际取用的字段对齐）。
_SOURCES: tuple[tuple[str, tuple[Field, ...]], ...] = (
    ("营业收入", (Field.REVENUE,)),
    ("净利润", (Field.NET_INCOME,)),
    ("总资产", (Field.TOTAL_ASSETS,)),
    ("所有者权益", (Field.EQUITY, Field.EQUITY_PARENT)),
)


def _provenance(stmts, fields: tuple[Field, ...]) -> str:
    """某个数取自哪张表、原始科目名是什么、来自哪一列。找不到就明说。"""
    for name, st in (("利润表", getattr(stmts, "income", None)),
                     ("资产负债表", getattr(stmts, "balance", None)),
                     ("现金流量表", getattr(stmts, "cash_flow", None))):
        if st is None:
            continue
        for row in getattr(st, "rows", []):
            if getattr(row, "field", None) in fields and getattr(row, "value", None) is not None:
                col = (st.columns[0] if getattr(st, "columns", None) else "")
                bits = [f"{name}", f"原表科目「{row.label}」"]
                if col:
                    bits.append(f"数值列「{col}」")
                if getattr(row, "via", ""):
                    bits.append(f"解析路径 {row.via}")
                return " · ".join(bits)
    return "**没定位到**（这个数在材料里没认出来，报告不替它编来源）"


def build_report(stmts, dup, *, target: str, generated_at: str,
                 version: str) -> str:
    """生成财务分析报告（纯文本，正式口径）。

    `dup` 是 `valuation.dupont.analyse()` 的结果；`stmts` 是三张表。
    """
    lines: list[str] = []
    add = lines.append

    add("财务分析报告")
    # 财务分析**没有问答步**（这是它便宜的原因），所以这里给的是**材料名**，
    # 不是工商登记名 —— 写"标的"会让人以为工具查了工商信息。
    add(f"材料：{target}")
    add(f"生成：{generated_at} · freeanalyst {version}")
    add("=" * 72)

    # ── 一、口径 ─────────────────────────────────────────────
    add("")
    add("一、材料与口径")
    add("")
    for name, st in (("资产负债表", getattr(stmts, "balance", None)),
                     ("利润表", getattr(stmts, "income", None)),
                     ("现金流量表", getattr(stmts, "cash_flow", None))):
        if st is None:
            add(f"  {name}：**没识别到**")
            continue
        cols = "、".join(getattr(st, "columns", []) or []) or "未标注"
        # 从 PDF 抽出来的表，这里存的其实是**页范围**而不是列名 ——
        # 写成"数值列"会让人以为材料里真有个叫「第89—91页」的列（真别扭过）。
        tag = "页范围" if "页" in cols else "数值列"
        add(f"  {name}：{len(getattr(st, 'rows', []))} 行 · "
            f"{len(getattr(st, 'fields', {}) or {})} 个科目 · {tag}「{cols}」")
    add(f"  金额单位：{dup.unit or '未确定'}")
    add(f"  期间：{dup.years or '未确定'}")

    missing = []
    try:
        missing = list(stmts.missing_key_fields)
    except Exception:                                    # noqa: BLE001
        pass
    add(f"  关键科目缺口：{('缺 ' + '、'.join(missing)) if missing else '无'}")
    add("    （口径说明：这里看的是「估值要用的那几个数齐了没」，"
        "不是「有多少行没映射上」—— 后者会被表头和 IFRS 专有项灌满，估值一个都用不到。）")

    # ── 二、经营回报 ─────────────────────────────────────────
    add("")
    add("二、经营回报（杜邦分解）")
    add("")
    for ln in dup.as_lines():
        add(f"  {ln}" if not ln.startswith("  ") else ln)

    # ── 三、输入与来源 ───────────────────────────────────────
    add("")
    add("三、输入与来源（复核用）")
    add("")
    for label, fields in _SOURCES:
        v = dup.inputs.get(label)
        shown = "**缺**" if v is None else f"{v:,.2f}"
        add(f"  {label:<10} {shown}")
        add(f"      ← {_provenance(stmts, fields)}")
    for label in ("利润总额",):
        if label in dup.inputs:
            add(f"  {label:<10} {dup.inputs[label]:,.2f}")

    # ── 四、提示 ─────────────────────────────────────────────
    add("")
    add("四、提示")
    add("")
    add("  · 本报告**不含任何假设** —— 它只读材料里的三张表，"
        "不取任何网上数据，也不做任何预测。")
    add("  · 算不出来的项一律**留空并说明**，不填 0、不替代。")
    for st_name, st in (("资产负债表", getattr(stmts, "balance", None)),
                        ("利润表", getattr(stmts, "income", None)),
                        ("现金流量表", getattr(stmts, "cash_flow", None))):
        for c in getattr(st, "conflicts", []) or []:
            add(f"  · ⚠️ {st_name}：{c}")
    for n in dup.notes:
        add(f"  · ⚠️ {n}")
    add("")
    add("─" * 72)
    add("本报告由 freeanalyst 在本机生成。材料不出本机。")
    return "\n".join(lines) + "\n"
