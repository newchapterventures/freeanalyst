"""WACC / CAPM —— 折现率不是拍出来的。

这是估值里最容易被随手填一个数的环节，也是影响最大的环节之一。
所以这里把每一项拆开，逐项要求来源。

    WACC = E/(D+E) × Re + D/(D+E) × Rd × (1 − t)
    Re   = Rf + β_L × ERP + 规模溢价 + 国家风险溢价

对非上市公司，还必须加：
  - 规模溢价（小公司风险更高）
  - 流动性折价通常不放在 WACC 里，而是在股权价值上单独打折（见 core 的说明）

参考：Damodaran 的国别风险溢价与规模溢价方法。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .core import Assumption, Confidence, Trace

# 规模溢价参考值（Damodaran 口径，按市值分档）
# 市场价值越小，溢价越高。非上市公司通常落在最低几档。
SIZE_PREMIUM_BY_DECILE = {
    1: 0.0525,  # 微型
    2: 0.0424,
    3: 0.0368,
    4: 0.0310,
    5: 0.0266,
    6: 0.0220,
    7: 0.0168,
    8: 0.0110,
    9: 0.0060,
    10: 0.0000,  # 大盘
}


@dataclass
class WaccInputs:
    """WACC 的九项输入，每一项都要来源。"""

    risk_free: Assumption          # 无风险利率（对应币种与期限）
    equity_risk_premium: Assumption  # 股权风险溢价（成熟市场）
    beta_unlevered: Assumption     # 去杠杆 beta（同行业可比公司）
    tax_rate: Assumption           # 边际所得税率
    cost_of_debt: Assumption       # 债务成本（有实际借款利率就用实际）
    debt: Assumption               # 有息负债（市值口径，无市值用账面近似）
    equity: Assumption             # 股权市值（非上市用估值倒推，需标注）
    size_premium: Assumption | None = None       # 规模溢价
    country_risk_premium: Assumption | None = None  # 国家风险溢价

    def all_assumptions(self) -> list[Assumption]:
        out = [self.risk_free, self.equity_risk_premium, self.beta_unlevered,
               self.tax_rate, self.cost_of_debt, self.debt, self.equity]
        if self.size_premium:
            out.append(self.size_premium)
        if self.country_risk_premium:
            out.append(self.country_risk_premium)
        return out


@dataclass
class WaccResult:
    wacc: float
    cost_of_equity: float
    beta_levered: float
    equity_weight: float
    debt_weight: float
    after_tax_cost_of_debt: float
    trace: Trace
    assumptions: list[Assumption] = field(default_factory=list)


def relever_beta(beta_unlevered: float, debt: float, equity: float, tax_rate: float) -> float:
    """Hamada 公式：把可比公司的去杠杆 beta 还原到本公司的资本结构。

        β_L = β_U × [1 + (1 − t) × D/E]
    """
    if equity <= 0:
        raise ValueError("股权价值必须为正，否则 D/E 无意义")
    return beta_unlevered * (1 + (1 - tax_rate) * (debt / equity))


def unlever_beta(beta_levered: float, debt: float, equity: float, tax_rate: float) -> float:
    """反向：从可比公司的杠杆 beta 推出无杠杆 beta。"""
    if equity <= 0:
        raise ValueError("股权价值必须为正")
    return beta_levered / (1 + (1 - tax_rate) * (debt / equity))


def _v(a: Assumption, default: float | None = None) -> float:
    """取出假设的数值。缺失即报错——不许静默用默认值。"""
    if a.value is None:
        if default is not None:
            return default
        raise ValueError(f"假设「{a.name}」缺失，无法继续计算")
    return float(a.value)


def compute_wacc(inputs: WaccInputs) -> WaccResult:
    """计算 WACC。任何缺项直接报错 —— 不许静默用默认值。"""
    missing = [a.name for a in inputs.all_assumptions() if a.is_missing]
    if missing:
        raise ValueError(f"以下假设缺失，无法计算 WACC：{', '.join(missing)}")

    rf = _v(inputs.risk_free)
    erp = _v(inputs.equity_risk_premium)
    beta_u = _v(inputs.beta_unlevered)
    tax = _v(inputs.tax_rate)
    rd = _v(inputs.cost_of_debt)
    debt = _v(inputs.debt)
    equity = _v(inputs.equity)
    sp = _v(inputs.size_premium, 0.0) if inputs.size_premium else 0.0
    crp = _v(inputs.country_risk_premium, 0.0) if inputs.country_risk_premium else 0.0

    trace = Trace()
    trace.add("去杠杆 beta", f"β_U = {beta_u:.3f}")
    trace.add("资本结构", f"D/E = {debt:,.0f}/{equity:,.0f} = {debt/equity:.4f}")

    beta_l = relever_beta(beta_u, debt, equity, tax)
    trace.add("杠杆 beta", f"β_L = β_U × [1 + (1−{tax:.2%}) × {debt/equity:.4f}] = {beta_l:.4f}")

    re = rf + beta_l * erp + sp + crp
    trace.add("股权成本", f"Re = {rf:.4%} + {beta_l:.4f}×{erp:.4%} + {sp:.4%} + {crp:.4%} = {re:.4%}")

    total = debt + equity
    we, wd = equity / total, debt / total
    trace.add("权重", f"E/V = {we:.2%}   D/V = {wd:.2%}")

    rd_at = rd * (1 - tax)
    trace.add("税后债务成本", f"Rd×(1−t) = {rd:.4%} × (1−{tax:.2%}) = {rd_at:.4%}")

    wacc = we * re + wd * rd_at
    trace.add("WACC", f"{we:.2%}×{re:.4%} + {wd:.2%}×{rd_at:.4%} = {wacc:.4%}")

    return WaccResult(
        wacc=wacc,
        cost_of_equity=re,
        beta_levered=beta_l,
        equity_weight=we,
        debt_weight=wd,
        after_tax_cost_of_debt=rd_at,
        trace=trace,
        assumptions=inputs.all_assumptions(),
    )


def sensitivity_grid(
    inputs: WaccInputs,
    wacc_deltas: tuple[float, ...] = (-0.02, -0.01, 0.0, 0.01, 0.02),
) -> list[tuple[float, float]]:
    """WACC 敏感性：围绕基准上下扰动，返回 (WACC, 股权成本) 对照。"""
    base = compute_wacc(inputs)
    return [(base.wacc + d, base.cost_of_equity + d) for d in wacc_deltas]
