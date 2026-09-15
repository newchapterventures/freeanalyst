"""DCF 引擎 —— 含反向估值（Reverse DCF）。

正向：给假设 → 出估值。
反向：给价格 → 反推隐含假设。  ← 尽调里最有用的一件工具

现金流口径用 FCFF（企业自由现金流），因为它和 WACC 匹配：

    FCFF = EBIT × (1 − t) + D&A − CapEx − ΔNWC
         = EBITDA × (1 − t) + D&A × t − CapEx − ΔNWC

然后：

    企业价值 EV = Σ FCFF_t / (1+WACC)^t  +  终值 / (1+WACC)^n
    股权价值     = EV − 净债务 − 少数股东权益 + 非经营性资产

**注意**：EBITDA 口径和 D&A 的处理必须一致，不然会重复扣减。
这个模块用的是"从 EBITDA 出发，加回 D&A×t"的写法，等价于先减 D&A 再加回，
但少一步运算、少一个出错点。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .core import Assumption, Confidence, Scenario, Trace, ValuationResult
from .wacc import WaccResult, compute_wacc


@dataclass
class DcfInputs:
    """DCF 的全部输入。逐年给驱动因子，因为这样每一步都可追溯。"""

    scenario: Scenario
    years: list[int]
    base_revenue: Assumption           # 上一个实际年度的收入（算起始营运资本用）
    revenue: list[Assumption]          # 与 years 等长，单位一致（万元/百万元）
    ebitda_margin: list[Assumption]    # 小数，如 0.245
    tax_rate: Assumption
    da_pct_revenue: Assumption         # 折旧摊销占收入比
    capex_pct_revenue: Assumption      # 资本开支占收入比
    nwc_pct_revenue: Assumption        # 营运资本占收入比（期末余额口径）
    terminal_growth: Assumption        # 永续增长率 g
    net_debt: Assumption               # 有息负债 − 现金
    minority_interest: Assumption = field(
        default_factory=lambda: Assumption("少数股东权益", 0.0, "万元", "无", Confidence.HIGH)
    )
    non_operating_assets: Assumption = field(
        default_factory=lambda: Assumption("非经营性资产", 0.0, "万元", "无", Confidence.HIGH)
    )
    mid_year: bool = False             # 是否用年中折现惯例

    def all_assumptions(self) -> list[Assumption]:
        return [
            self.base_revenue, *self.revenue, *self.ebitda_margin, self.tax_rate,
            self.da_pct_revenue, self.capex_pct_revenue, self.nwc_pct_revenue,
            self.terminal_growth, self.net_debt,
            self.minority_interest, self.non_operating_assets,
        ]


@dataclass
class _YearRow:
    year: int
    revenue: float
    ebitda: float
    fcff: float
    discount_factor: float
    pv: float
    nwc: float


def _v(a: Assumption, default: float | None = None) -> float:
    """取数值。默认值只允许桥梁项显式传，核心输入缺失必须报错。"""
    if a.value is None:
        if default is not None:
            return default
        raise ValueError(f"假设「{a.name}」缺失（来源：{a.source}），无法继续计算")
    return float(a.value)


def project(inputs: DcfInputs, wacc: float) -> tuple[list[_YearRow], float, float]:
    """跑出现金流表。返回 (逐年明细, 明确预测期现值合计, 期末 FCFF)。"""
    if not (len(inputs.years) == len(inputs.revenue) == len(inputs.ebitda_margin)):
        raise ValueError("years / revenue / ebitda_margin 长度必须一致")

    tax = _v(inputs.tax_rate)
    da_r = _v(inputs.da_pct_revenue)
    capex_r = _v(inputs.capex_pct_revenue)
    nwc_r = _v(inputs.nwc_pct_revenue)

    rows: list[_YearRow] = []
    # 起始营运资本基于「上一个实际年度」的收入，不是预测第一年。
    # 用错基数会让第一年的 ΔNWC 变成 0，低估现金流出。
    prev_nwc = _v(inputs.base_revenue) * nwc_r
    pv_sum = 0.0

    for i, year in enumerate(inputs.years):
        rev = _v(inputs.revenue[i])
        ebitda = rev * _v(inputs.ebitda_margin[i])
        da = rev * da_r
        capex = rev * capex_r
        nwc = rev * nwc_r
        d_nwc = nwc - prev_nwc

        fcff = ebitda * (1 - tax) + da * tax - capex - d_nwc

        t = i + 1 - (0.5 if inputs.mid_year else 0.0)
        df = 1.0 / ((1 + wacc) ** t)
        pv = fcff * df
        pv_sum += pv

        rows.append(_YearRow(year, rev, ebitda, fcff, df, pv, nwc))
        prev_nwc = nwc

    return rows, pv_sum, rows[-1].fcff


def enterprise_value(
    inputs: DcfInputs,
    wacc: float,
    use_exit_multiple: float | None = None,
) -> tuple[float, Trace, list[_YearRow]]:
    """算企业价值。终值可用永续增长法或退出倍数法。"""
    rows, pv_sum, fcff_n = project(inputs, wacc)
    n = len(rows)
    trace = Trace()

    trace.add("折现率", f"WACC = {wacc:.4%}")

    if use_exit_multiple is not None:
        tv = rows[-1].ebitda * use_exit_multiple
        trace.add("终值（退出倍数法）", f"{rows[-1].ebitda:,.0f} × {use_exit_multiple:.2f}x = {tv:,.0f}")
    else:
        g = _v(inputs.terminal_growth)
        if g >= wacc:
            raise ValueError(f"永续增长率 {g:.2%} 不得大于等于 WACC {wacc:.2%}，否则终值发散")
        tv = fcff_n * (1 + g) / (wacc - g)
        trace.add("终值（永续增长法）", f"{fcff_n:,.0f} × (1+{g:.2%}) / ({wacc:.2%}−{g:.2%}) = {tv:,.0f}")

    t_n = n - (0.5 if inputs.mid_year else 0.0)
    pv_tv = tv / ((1 + wacc) ** t_n)
    trace.add("终值现值", f"{tv:,.0f} / (1+{wacc:.2%})^{t_n:.1f} = {pv_tv:,.0f}")

    trace.add("明确期现值", f"Σ PV(FCFF) = {pv_sum:,.0f}")
    ev = pv_sum + pv_tv
    trace.add("企业价值", f"{pv_sum:,.0f} + {pv_tv:,.0f} = {ev:,.0f}")

    return ev, trace, rows


def equity_bridge(ev: float, inputs: DcfInputs, trace: Trace) -> float:
    """企业价值 → 股权价值。这里是最容易漏项的地方。"""
    nd = _v(inputs.net_debt)                       # 核心输入，缺失即报错
    mi = _v(inputs.minority_interest, 0.0)         # 桥梁项，缺失按 0 但标记缺口
    noa = _v(inputs.non_operating_assets, 0.0)

    trace.add("减：净债务", f"− {nd:,.0f}")
    if mi:
        trace.add("减：少数股东权益", f"− {mi:,.0f}")
    if noa:
        trace.add("加：非经营性资产", f"+ {noa:,.0f}")

    eq = ev - nd - mi + noa
    trace.add("股权价值", f"{eq:,.0f}")

    missing_bridge = [a.name for a in (inputs.minority_interest, inputs.non_operating_assets)
                      if a.is_missing]
    if missing_bridge:
        trace.add("⚠️ 桥梁项按 0 处理",
                  "缺失项：" + "、".join(missing_bridge) + " —— 已计入数据缺口，需补齐后重算")

    return eq


def run_dcf(
    inputs: DcfInputs,
    wacc: float,
    use_exit_multiple: float | None = None,
    range_grid: tuple[list[float], list[float]] | None = None,
) -> ValuationResult:
    """跑完整 DCF，返回带追溯的结果。

    range_grid 给 (WACC 序列, 永续增长率序列) 时，区间取敏感性网格的极值——
    单点 DCF 报成 "X – X" 是没有信息量的。grid 里的 nan（不可行格）会被跳过。
    """
    ev, trace, rows = enterprise_value(inputs, wacc, use_exit_multiple)
    eq = equity_bridge(ev, inputs, trace)

    share = _pv_tv_share(inputs, wacc, use_exit_multiple)

    low = high = eq
    if range_grid:
        grid = sensitivity(inputs, range_grid[0], range_grid[1])
        vals = [v for row in grid for v in row if v == v]  # 跳过 nan
        if vals:
            low, high = min(vals), max(vals)
            trace.add("区间来源", f"敏感性网格极值（{len(vals)} 个可行格）")

    notes = [
        f"明确预测期 {len(rows)} 年，起始年 {rows[0].year}",
        f"终值占企业价值 {share:.1%}",
    ]
    if range_grid:
        notes.append(
            f"区间为敏感性网格极值，中枢为基准情景。集中度："
            f"{low:,.0f} – {high:,.0f} 万元，跨度 {(high - low) / eq:.0%}。"
        )
        if (high - low) / eq > 0.5:
            notes.append(
                "⚠️ 敏感性跨度超过中值的 50%：这个模型对折现率和永续增长极为敏感，"
                "单点结论不可靠。谈判时应给出区间而非一个数字。"
            )
    if share > 0.75:
        notes.append(
            "⚠️ 终值占比超过 75%：这个估值主要由「永续」那一段撑着。"
            "建议同时用退出倍数法交叉验证（配置里加 exit_multiple）。"
        )

    return ValuationResult(
        method="DCF（FCFF）",
        low=low, mid=eq, high=high,
        unit="万元",
        trace=trace,
        assumptions=inputs.all_assumptions(),
        notes=notes,
    )


def _pv_tv_share(inputs: DcfInputs, wacc: float, exit_multiple: float | None) -> float:
    ev, _, _ = enterprise_value(inputs, wacc, exit_multiple)
    rows, pv_sum, fcff_n = project(inputs, wacc)
    n = len(rows)
    if exit_multiple is not None:
        tv = rows[-1].ebitda * exit_multiple
    else:
        g = _v(inputs.terminal_growth)
        tv = fcff_n * (1 + g) / (wacc - g)
    t_n = n - (0.5 if inputs.mid_year else 0.0)
    return (tv / ((1 + wacc) ** t_n)) / ev


def sensitivity(
    inputs: DcfInputs,
    wacc_range: list[float],
    growth_range: list[float],
) -> list[list[float]]:
    """二维敏感性矩阵：WACC × 永续增长率 → 股权价值。

    这是"哪个假设最影响结果"的可视化。没有敏感性的 DCF 是不完整的。
    """
    grid: list[list[float]] = []
    for w in wacc_range:
        row: list[float] = []
        for g in growth_range:
            if g >= w:
                row.append(float("nan"))
                continue
            tmp = DcfInputs(
                scenario=inputs.scenario, years=inputs.years,
                base_revenue=inputs.base_revenue,
                revenue=inputs.revenue, ebitda_margin=inputs.ebitda_margin,
                tax_rate=inputs.tax_rate, da_pct_revenue=inputs.da_pct_revenue,
                capex_pct_revenue=inputs.capex_pct_revenue,
                nwc_pct_revenue=inputs.nwc_pct_revenue,
                terminal_growth=Assumption("永续增长率", g, "", "敏感性扫描", Confidence.MEDIUM),
                net_debt=inputs.net_debt, minority_interest=inputs.minority_interest,
                non_operating_assets=inputs.non_operating_assets, mid_year=inputs.mid_year,
            )
            ev, _t, _r = enterprise_value(tmp, w)
            row.append(equity_bridge(ev, tmp, Trace()))
        grid.append(row)
    return grid


# --------------------------------------------------------------------------
# 反向估值 —— 尽调里最有用的一件工具
# --------------------------------------------------------------------------

@dataclass
class ReverseDcfResult:
    target_equity_value: float
    solved_growth: float | None
    solved_wacc: float | None
    solved_margin_delta: float | None
    trace: Trace
    feasible: bool
    note: str


def reverse_dcf_growth(
    inputs: DcfInputs,
    wacc: float,
    target_equity_value: float,
    lo: float = -0.30,
    hi: float = 1.00,
    tol: float = 1e-6,
) -> ReverseDcfResult:
    """给定价格，反推隐含的永续增长率。

    用法：对方要 4.2 亿 —— 这背后假设的永续增长是多少？现实吗？
    这是谈判的切入点，也是识别"这个价格不可能兑现"的工具。
    """
    trace = Trace()
    trace.add("目标股权价值", f"{target_equity_value:,.0f} 万元")

    def equity_at(g: float) -> float:
        if g >= wacc:
            return math.inf
        tmp = DcfInputs(
            scenario=inputs.scenario, years=inputs.years,
            base_revenue=inputs.base_revenue,
            revenue=inputs.revenue, ebitda_margin=inputs.ebitda_margin,
            tax_rate=inputs.tax_rate, da_pct_revenue=inputs.da_pct_revenue,
            capex_pct_revenue=inputs.capex_pct_revenue,
            nwc_pct_revenue=inputs.nwc_pct_revenue,
            terminal_growth=Assumption("永续增长率", g, "", "反向求解", Confidence.MEDIUM),
            net_debt=inputs.net_debt, minority_interest=inputs.minority_interest,
            non_operating_assets=inputs.non_operating_assets, mid_year=inputs.mid_year,
        )
        ev, _t, _r = enterprise_value(tmp, wacc)
        return equity_bridge(ev, tmp, Trace())

    lo_v, hi_v = equity_at(lo), equity_at(min(hi, wacc - 1e-4))

    if not (lo_v <= target_equity_value <= hi_v):
        trace.add("结论", f"在 g ∈ [{lo:.0%}, {hi:.0%}] 区间内无解")
        return ReverseDcfResult(
            target_equity_value, None, None, None, trace, False,
            f"目标价格超出可行区间：该区间对应的股权价值是 {lo_v:,.0f} – {hi_v:,.0f} 万元。"
            f"说明这个价格不是靠永续增长假设撑起来的，要看明确预测期或退出倍数。",
        )

    a, b = lo, min(hi, wacc - 1e-4)
    for _ in range(200):
        m = (a + b) / 2
        if abs(equity_at(m) - target_equity_value) < tol * max(1.0, target_equity_value):
            break
        if equity_at(m) < target_equity_value:
            a = m
        else:
            b = m
    g_implied = (a + b) / 2

    trace.add("隐含永续增长率", f"g = {g_implied:.4%}")
    trace.add("校验", f"以 g={g_implied:.4%} 重算，股权价值 {equity_at(g_implied):,.0f} 万元")

    return ReverseDcfResult(
        target_equity_value, g_implied, None, None, trace, True,
        f"该价格隐含永续增长率 {g_implied:.2%}。请判断这个增速是否现实——"
        f"它意味着企业要永远以这个速度增长下去。",
    )
