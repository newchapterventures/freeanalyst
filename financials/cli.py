"""把报表集接进 `value.py` 的报告流。

配置里加一节 `statements`，估值报告就会多出一段：

```json
"statements": {
  "label": "Fitbit FY2016 10-K",
  "unit": "千美元",
  "as_of": "2016-12-31",
  "balance_sheet": "materials/fitbit-2016-10k/R2.htm",
  "income_statement": "materials/fitbit-2016-10k/R4.htm",
  "cash_flow": "materials/fitbit-2016-10k/R8.htm",
  "gaap": "US GAAP", "scope": "合并", "audited": "已审计"
}
```

## 报告里说三件事，缺一不可

1. **勾稽校验结果** —— 平了才敢用这些数（§4.5）
2. **哪些是自动填的** —— 事实类；附构成明细，能核对
3. **哪些必须你自己填** —— 假设类；附历史参考值，但**不代填**

第 3 条是这个模块存在的意义。把历史比率自动填成预测假设，
用户会以为那是自己的判断 —— **那正好是这个产品要避免的事**。
"""

from __future__ import annotations

from pathlib import Path

from . import statements as stm


class StatementConfigError(Exception):
    """配置写错了。"""


def _resolve(path_str: str, base: Path) -> Path:
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else (base / p)


def load_from_config(cfg: dict, base: Path) -> stm.Statements:
    sec = cfg.get("statements")
    if not isinstance(sec, dict):
        raise StatementConfigError("statements 必须是对象")

    unit = sec.get("unit", "")
    s = stm.Statements(
        gaap=sec.get("gaap", ""),
        scope=sec.get("scope", ""),
        audited=sec.get("audited", ""),
        period=sec.get("period", sec.get("as_of", "")),
    )

    wanted = {
        "balance_sheet": "balance",
        "income_statement": "income",
        "cash_flow": "cash_flow",
    }
    for key, attr in wanted.items():
        rel = sec.get(key)
        if not rel:
            continue
        path = _resolve(rel, base)
        if not path.exists():
            raise StatementConfigError(f"statements.{key} 指向的文件不存在：{path}")
        setattr(s, attr, stm.load_one(path, key, unit))

    if s.balance is None and s.income is None and s.cash_flow is None:
        raise StatementConfigError(
            "statements 里至少要给一张表"
            "（balance_sheet / income_statement / cash_flow）")

    # 口径四项没声明就提醒 —— §4.1–4.4 要求「先标注，后调整」
    for label, val in (("准则（gaap）", s.gaap), ("合并口径（scope）", s.scope),
                       ("审计状态（audited）", s.audited)):
        if not val:
            s.warnings.append(f"{label}未声明 —— 准则/口径不同的报表不可直接比较")
    return s


def run_statements(cfg: dict, out: list[str], base: Path) -> stm.Statements:
    """把报表集的分析追加到 `out`。返回报表集供调用方继续用。"""
    s = load_from_config(cfg, base)

    out.append("\n" + "─" * 74)
    out.append("三张表解析")
    out.append("─" * 74)
    out.append(f"  口径：{s.gaap or '未声明'} · {s.scope or '未声明'} · "
               f"{s.audited or '未声明'} · {s.period or '期间未声明'}")

    for name, st in (("资产负债表", s.balance), ("利润表", s.income),
                     ("现金流量表", s.cash_flow)):
        if st is not None:
            matched = sum(1 for r in st.rows if r.field is not None)
            out.append(f"  {name}：{len(st.rows)} 行，映射上 {matched} 行"
                       f"（{Path(st.source).name}）")

    # ---------- 勾稽校验 ----------
    out.append("\n  【勾稽校验 —— 三条全平才说明映射对了】")
    checks = s.checks()
    for c in checks:
        out.append("  " + c.render("").replace("\n", "\n  "))
    failed = [c for c in checks if c.ok is False]
    unknown = [c for c in checks if c.ok is None and c.applicable]
    skipped = [c for c in checks if not c.applicable]
    if failed:
        out.append("\n  ⚠ **有勾稽不平 —— 上面的推算结果不要用。**")
        out.append("     差额能帮你定位是哪一行归属错了："
                   "先怀疑最后加进去的那几个科目。")
    elif unknown:
        out.append(f"\n  ⚠ 有 {len(unknown)} 条判不了（缺科目），"
                   "那些推算结果按缺口处理")
    elif skipped:
        out.append(f"\n  ✓ 能判的都平了。（{len(skipped)} 条不适用这项报表格式）")
    else:
        out.append("\n  ✓ 三条全平。下面的推算结果可以用于估值。")

    # ---------- 事实类：自动填 ----------
    out.append("\n  【事实类 —— 已自动填入】")
    for key, d in s.facts().items():
        out.append(f"    {d.render()}")
        if d.value is None:
            out.append(f"      （DCF 里与 `{key}` 对应的输入仍需你提供）")

    # ---------- 假设类：只给参考 ----------
    hist = s.history()
    out.append("\n  【假设类 —— 不代填，只给历史参考】")
    for k, v in hist.items():
        out.append(f"    {k:26} {'数据不足' if v is None else format(v, ',.4f')}")
    out.append("    **历史比率不等于预测假设。** 填多少是你的判断，"
               "引擎不替你决定。")

    if s.warnings:
        out.append("\n  口径声明缺口：")
        for w in s.warnings:
            out.append(f"    · {w}")

    return s


def apply_facts(cfg: dict, s: stm.Statements) -> list[str]:
    """把能推的事实填进 `cfg["dcf"]` 和 `cfg["multiples"]`，返回填了哪些。

    **只填能算出来的。** 算不出来的原样留着，让缺数据在引擎那里
    以「缺」的形式暴露出来，而不是被一个假数盖住。

    ## 两个节都要填

    乘数法和 DCF 都需要净债务（企业价值 → 股权价值）。只填 dcf
    会让乘数法那边缺一个本来能算出来的输入。

    ## 填的是对象不是裸数字

    `value.py` 的 `A()` 两种都吃，但**裸数字会丢掉来源**，
    被标成「未注明（裸数字）」+ 低置信度。推算出来的东西有来源，
    必须带上 —— 追溯是这个产品的底线。

    单位也一起带上：报表是千美元，`value.py` 的默认单位是万元，
    不带单位会把千美元的数标成万元。
    """
    unit = cfg.get("unit") or (s.balance.unit if s.balance else "") or ""
    conf = "高" if _checks_all_flat(s) else "中"
    facts = s.facts()
    filled: list[str] = []

    # dcf 与 multiples 用同一套键名
    keys = ("base_revenue", "net_debt", "minority_interest")
    for section in ("dcf", "multiples"):
        target = cfg.get(section)
        if not isinstance(target, dict):
            continue
        for key in keys:
            d = facts.get(key)
            if d is None or d.value is None or key in target:
                continue
            parts = " ".join(f"{n} {v:,.0f}" for n, v in d.parts)
            source = f"由三张表推算（{s.period or '当期'}）：{parts}"
            if d.note:
                source += f"；{d.note}"
            target[key] = {"value": d.value, "unit": unit,
                           "source": source, "confidence": conf}
            filled.append(f"{section}.{key} = {d.value:,.4f}（{unit}）  ← {source}")
    return filled


def _checks_all_flat(s: stm.Statements) -> bool:
    """勾稽三条全平才敢给高置信度。

    **不平的时候推算结果本身就是可疑的** —— 至少有一行归属错了，
    而错的那一行很可能就在这个口径的构成里。
    """
    checks = s.checks()
    return bool(checks) and all(c.ok is True for c in checks)
