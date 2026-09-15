#!/usr/bin/env python3
"""估值引擎的命令行入口。

    python3 value.py examples/valuation-demo.json

设计取舍：**用配置文件驱动，不用 LLM 算数。**

估值是确定性数学。让大模型去算会算错，而且算错了你查不出来。
这个脚本读一份 JSON，跑纯代码计算，输出带完整追溯的报告。

模型在这个流程里的位置是另外两块：
  1. 把财报/规划抽成这份 JSON（v0.2e）
  2. 给每个假设找对照系（v0.3 假设参谋）

JSON 里每个数字可以有两种写法：

    "revenue": [21200, 24500]                       # 裸数字 → 标为低置信度
    "ebitda_margin": [{"value": 0.245,
                       "source": "管理层规划 p.12",
                       "confidence": "低"}, 0.25]    # 带来源
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from valuation import (
    Assumption,
    Confidence,
    DcfInputs,
    MultiplesInputs,
    Purpose,
    Scenario,
    SdeBuild,
    Stage,
    Stance,
    ValuationResult,
    build_sde,
    comps_summary,
    run_dcf,
    run_multiples,
    reverse_dcf_growth,
    sensitivity,
    compute_wacc,
    WaccInputs,
)

CONF_MAP = {"高": Confidence.HIGH, "中": Confidence.MEDIUM,
            "低": Confidence.LOW, "缺失": Confidence.MISSING}

UNIT = "万元"


def A(spec, name: str) -> Assumption:
    """把 JSON 里的一个数字或对象转成 Assumption。

    裸数字会被标为「低置信度」——因为没有来源。这是有意的：
    没有来源的假设就是低置信度的假设，不该被当成事实。
    """
    if isinstance(spec, dict):
        return Assumption(
            name=name,
            value=spec.get("value"),
            unit=spec.get("unit", UNIT),
            source=spec.get("source", "未注明"),
            confidence=CONF_MAP.get(spec.get("confidence", "中"), Confidence.MEDIUM),
            note=spec.get("note", ""),
        )
    if spec is None:
        return Assumption(name, None, UNIT, "未提供", Confidence.MISSING)
    return Assumption(name, float(spec), UNIT, "未注明（裸数字）", Confidence.LOW)


def As(items, prefix: str) -> list[Assumption]:
    return [A(x, f"{prefix}[{i}]") for i, x in enumerate(items)]


def build_scenario(raw: dict) -> Scenario:
    return Scenario(
        purpose=Purpose(raw.get("purpose", "并购定价")),
        stance=Stance(raw.get("stance", "买方")),
        stage=Stage(raw.get("stage", "成熟企业")),
        valuation_date=raw.get("valuation_date", "未指定"),
        currency=raw.get("currency", "CNY"),
        equity_scope=raw.get("equity_scope", "100%"),
    )


def hr(title: str = "", char: str = "─") -> str:
    if not title:
        return char * 74
    return f"\n{title}\n" + char * 74


def render_result(r: ValuationResult, show_trace: bool = True) -> str:
    lines = [
        f"  区间        {r.range_text}",
    ]
    if show_trace:
        lines.append("\n  计算追溯：")
        lines.append(r.trace.render())
    if r.notes:
        lines.append("\n  提示：")
        for n in r.notes:
            lines.append(f"    · {n}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="估值引擎")
    ap.add_argument("config", help="估值输入 JSON")
    ap.add_argument("--no-trace", action="store_true", help="不打印计算追溯")
    args = ap.parse_args()

    path = Path(args.config)
    if not path.exists():
        print(f"找不到配置文件：{path}", file=sys.stderr)
        return 1

    cfg = json.loads(path.read_text(encoding="utf-8"))
    sc = build_scenario(cfg.get("scenario", {}))

    out: list[str] = []
    out.append("=" * 74)
    out.append(f"估值报告 —— {cfg.get('target', '未命名标的')}")
    out.append("=" * 74)
    out.append(f"场景：{sc.describe()}")
    if cfg.get("note"):
        out.append(f"说明：{cfg['note']}")

    results: list[ValuationResult] = []

    # ---------------- WACC（可选，只有 DCF 需要） ----------------
    wacc_val: float | None = None
    if "wacc" in cfg:
        w = cfg["wacc"]
        wi = WaccInputs(
            risk_free=A(w["risk_free"], "无风险利率"),
            equity_risk_premium=A(w["equity_risk_premium"], "股权风险溢价"),
            beta_unlevered=A(w["beta_unlevered"], "去杠杆beta"),
            tax_rate=A(w["tax_rate"], "所得税率"),
            cost_of_debt=A(w["cost_of_debt"], "债务成本"),
            debt=A(w["debt"], "有息负债"),
            equity=A(w["equity"], "股权价值"),
            size_premium=A(w["size_premium"], "规模溢价") if "size_premium" in w else None,
            country_risk_premium=A(w["country_risk_premium"], "国家风险溢价") if "country_risk_premium" in w else None,
        )
        wr = compute_wacc(wi)
        wacc_val = wr.wacc
        out.append(hr("折现率（WACC）"))
        out.append(f"  WACC = {wr.wacc:.2%}   （股权成本 {wr.cost_of_equity:.2%}，β_L {wr.beta_levered:.3f}）")
        if not args.no_trace:
            out.append("\n  计算追溯：")
            out.append(wr.trace.render())

    # ---------------- 乘数法 ----------------
    if "multiples" in cfg:
        m = cfg["multiples"]
        mi = MultiplesInputs(
            metric_name=m.get("metric_name", "EBITDA"),
            metric_value=A(m["metric_value"], m.get("metric_name", "EBITDA")),
            multiple_low=A(m["multiple_low"], "倍数下沿"),
            multiple_mid=A(m["multiple_mid"], "倍数中枢"),
            multiple_high=A(m["multiple_high"], "倍数上沿"),
            net_debt=A(m.get("net_debt"), "净债务"),
            minority_interest=A(m.get("minority_interest"), "少数股东权益"),
            non_operating_assets=A(m.get("non_operating_assets"), "非经营性资产"),
            discount_for_lack_of_marketability=(
                A(m["dlom"], "少数股权折价") if "dlom" in m else None),
        )
        r = run_multiples(mi)
        results.append(r)
        out.append(hr(f"方法一 · {r.method}"))
        out.append(render_result(r, not args.no_trace))

    # ---------------- SDE ----------------
    if "sde" in cfg:
        s = cfg["sde"]
        sde_obj, sde_trace, sde_ins = build_sde(SdeBuild(
            net_income=A(s["net_income"], "净利润"),
            interest=A(s.get("interest", 0), "利息支出"),
            taxes=A(s.get("taxes", 0), "所得税"),
            depreciation_amortization=A(s.get("depreciation_amortization", 0), "折旧摊销"),
            owner_compensation_actual=A(s.get("owner_compensation_actual", 0), "实发薪酬"),
            owner_compensation_market=A(s.get("owner_compensation_market", 0), "市场薪酬"),
            personal_expenses=A(s.get("personal_expenses", 0), "个人性质费用"),
            one_time_gains=A(s.get("one_time_gains", 0), "一次性收益"),
            one_time_losses=A(s.get("one_time_losses", 0), "一次性损失"),
        ))
        out.append(hr("SDE 构建（中小企业并购口径）"))
        out.append(f"  SDE = {sde_obj.value:,.0f} 万元")
        if not args.no_trace:
            out.append("\n  计算追溯：")
            out.append(sde_trace.render())

    # ---------------- DCF ----------------
    if "dcf" in cfg and wacc_val is not None:
        d = cfg["dcf"]
        di = DcfInputs(
            scenario=sc,
            years=d["years"],
            base_revenue=A(d["base_revenue"], "基期收入"),
            revenue=As(d["revenue"], "收入"),
            ebitda_margin=As(d["ebitda_margin"], "EBITDA率"),
            tax_rate=A(d["tax_rate"], "所得税率"),
            da_pct_revenue=A(d["da_pct_revenue"], "折旧摊销占比"),
            capex_pct_revenue=A(d["capex_pct_revenue"], "资本开支占比"),
            nwc_pct_revenue=A(d["nwc_pct_revenue"], "营运资本占比"),
            terminal_growth=A(d["terminal_growth"], "永续增长率"),
            net_debt=A(d["net_debt"], "净债务"),
            minority_interest=A(d.get("minority_interest"), "少数股东权益"),
            non_operating_assets=A(d.get("non_operating_assets"), "非经营性资产"),
            mid_year=bool(d.get("mid_year", False)),
        )
        r = run_dcf(di, wacc_val, range_grid=(
            d["sensitivity"]["wacc"], d["sensitivity"]["growth"]
        ) if "sensitivity" in d else None)
        results.append(r)
        out.append(hr("方法二 · DCF（FCFF）"))
        out.append(render_result(r, not args.no_trace))

        # 退出倍数法交叉验证（终值占比过高时尤其需要）
        if "exit_multiple" in d:
            r2 = run_dcf(di, wacc_val, use_exit_multiple=float(d["exit_multiple"]))
            out.append(hr(f"DCF 交叉验证 · 终值改用退出倍数 {d['exit_multiple']:.1f}x"))
            out.append(f"  股权价值 {r2.mid:,.0f} 万元")
            out.append(f"  与永续增长法差额 {r2.mid - r.mid:+,.0f} 万元"
                       f"（{(r2.mid - r.mid) / r.mid:+.1%}）")

        # 敏感性
        if "sensitivity" in d:
            s = d["sensitivity"]
            grid = sensitivity(di, s["wacc"], s["growth"])
            out.append(hr("敏感性 · WACC × 永续增长率 → 股权价值"))
            head = "         " + "".join(f"{g:>12.2%}" for g in s["growth"])
            out.append(head)
            for i, w in enumerate(s["wacc"]):
                cells = "".join(
                    f"{v:>12,.0f}" if v == v else f"{'n/a':>12}" for v in grid[i])
                out.append(f"  {w:>6.2%}{cells}")

        # 反向估值
        if "ask_price" in cfg:
            res = reverse_dcf_growth(di, wacc_val, float(cfg["ask_price"]))
            out.append(hr(f"反向估值 · 从对方要价反推假设（{cfg['ask_price']:,.0f} 万元）"))
            out.append(f"  {res.note}")
            if not args.no_trace:
                out.append("\n  计算追溯：")
                out.append(res.trace.render())

    # ---------------- 可比公司 ----------------
    if "peers" in cfg:
        p = cfg["peers"]
        stats, trace, assumptions = comps_summary(p["list"], p.get("metric", "ebitda"), p["multiple_key"])
        out.append(hr(f"可比公司汇总 · {p['multiple_key']}"))
        if stats:
            out.append(f"  样本 {stats['n']} 家")
            out.append(f"  P25 {stats['p25']:.2f}x   中位 {stats['median']:.2f}x   P75 {stats['p75']:.2f}x")
            out.append(f"  均值 {stats['mean']:.2f}x   区间 {stats['min']:.2f}x – {stats['max']:.2f}x")
        if not args.no_trace:
            out.append("\n  计算追溯：")
            out.append(trace.render())

    # ---------------- 数据缺口与低置信度（强制输出） ----------------
    all_a: list[Assumption] = []
    for r in results:
        all_a.extend(r.assumptions)

    gaps = [a for a in all_a if a.is_missing]
    low = [a for a in all_a if a.confidence is Confidence.LOW]

    out.append(hr("数据缺口（必须补）", "═"))
    if gaps:
        for name in dict.fromkeys(a.name for a in gaps):   # 去重、保序
            out.append(f"  ✗ {name}")
    else:
        out.append("  无")

    out.append(hr("低置信度假设（结果的软肋）", "═"))
    if low:
        seen = set()
        for a in low:
            if a.name in seen:
                continue
            seen.add(a.name)
            out.append(f"  ! {a.name} = {a.value}  ← 来源：{a.source}")
    else:
        out.append("  无")

    # ---------------- 区间汇总 ----------------
    if len(results) > 1:
        out.append(hr("估值区间汇总（football field 的数据）", "═"))
        for r in results:
            out.append(f"  {r.method:<16} {r.range_text}")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
