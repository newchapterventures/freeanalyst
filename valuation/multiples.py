"""乘数法估值 —— EBITDA / SDE / 收入。

三条纪律：

1. **乘数必须带"什么时候、哪个市场、哪一组可比公司"三个限定。**
   一个没有来源的乘数就是一个意见。

2. **SDE 和 EBITDA 是两个口径，不能混用。**
   SDE（Seller's Discretionary Earnings）是中小企业并购的口径，
   含所有者薪酬调整；EBITDA 是公司口径。同一个乘数套到两个基数上，
   结果能差一倍。

3. **少数股东权益和少数股权折价是两回事。**
   前者是资产负债表科目（要扣除），后者是估值调整（要打折）。
   方向相反，混了会错两次。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .core import Assumption, Confidence, Trace, ValuationResult


def _v(a: Assumption, default: float | None = None) -> float:
    """取出假设的数值。

    **默认值只允许用在桥梁项上，而且调用方必须显式传。**
    核心输入（基数、乘数、净债务）缺失就必须报错 —— 静默填 0 会得出
    一个看起来正常、实际全错的结果。
    """
    if a.value is None:
        if default is not None:
            return default
        raise ValueError(f"假设「{a.name}」缺失（来源：{a.source}），无法继续计算")
    return float(a.value)


@dataclass
class MultiplesInputs:
    """乘数法估值的输入。基数 + 乘数 + 桥梁项。"""

    metric_name: str                    # "EBITDA" / "SDE" / "营业收入"
    metric_value: Assumption            # 基数
    multiple_low: Assumption            # 可比区间下沿
    multiple_mid: Assumption            # 中枢
    multiple_high: Assumption           # 上沿
    net_debt: Assumption = field(
        default_factory=lambda: Assumption("净债务", 0.0, "万元", "无", Confidence.HIGH))
    minority_interest: Assumption = field(
        default_factory=lambda: Assumption("少数股东权益", 0.0, "万元", "无", Confidence.HIGH))
    non_operating_assets: Assumption = field(
        default_factory=lambda: Assumption("非经营性资产", 0.0, "万元", "无", Confidence.HIGH))
    discount_for_lack_of_marketability: Assumption | None = None  # 少数股权折价（DLOM）
    #: 金额单位。**必须跟着配置走** —— 写死「万元」会把千美元的数标成万元。
    unit: str = "万元"

    def all_assumptions(self) -> list[Assumption]:
        out = [self.metric_value, self.multiple_low, self.multiple_mid,
               self.multiple_high, self.net_debt, self.minority_interest,
               self.non_operating_assets]
        if self.discount_for_lack_of_marketability:
            out.append(self.discount_for_lack_of_marketability)
        return out


def run_multiples(inputs: MultiplesInputs) -> ValuationResult:
    """乘数法估值。返回区间 + 完整追溯。"""
    metric = _v(inputs.metric_value)
    if metric <= 0:
        raise ValueError(
            f"{inputs.metric_name} 为 {metric}，不能作为乘数基数。"
            f"负或零基数的标的应改用其他方法（早期项目方法或资产法）。"
        )

    m_lo = _v(inputs.multiple_low)
    m_mid = _v(inputs.multiple_mid)
    m_hi = _v(inputs.multiple_high)
    if not (m_lo <= m_mid <= m_hi):
        raise ValueError(f"乘数区间不合法：{m_lo} / {m_mid} / {m_hi} 应递增")

    trace = Trace()
    _u = inputs.unit
    trace.add(f"{inputs.metric_name} 基数", f"{metric:,.0f} {_u}（来源：{inputs.metric_value.source}）")

    ev_lo = metric * m_lo
    ev_mid = metric * m_mid
    ev_hi = metric * m_hi
    trace.add("企业价值区间",
              f"{metric:,.0f} × [{m_lo:.2f}x, {m_hi:.2f}x] = {ev_lo:,.0f} – {ev_hi:,.0f} {_u}")

    nd = _v(inputs.net_debt)                      # 缺失即报错（核心输入）
    mi = _v(inputs.minority_interest, 0.0)        # 桥梁项，缺失按 0 处理但会被标为缺口
    noa = _v(inputs.non_operating_assets, 0.0)
    bridge = -nd - mi + noa
    trace.add("股权桥", f"− 净债务 {nd:,.0f} − 少数股东权益 {mi:,.0f} + 非经营性资产 {noa:,.0f} = {bridge:+,.0f}")
    if inputs.minority_interest.is_missing or inputs.non_operating_assets.is_missing:
        trace.add("⚠️ 桥梁项按 0 处理",
                  "缺失项：" + "、".join(
                      a.name for a in (inputs.minority_interest, inputs.non_operating_assets)
                      if a.is_missing) + " —— 已计入数据缺口，需补齐后重算")

    eq_lo, eq_mid, eq_hi = ev_lo + bridge, ev_mid + bridge, ev_hi + bridge

    notes: list[str] = []
    if inputs.discount_for_lack_of_marketability:
        dlom = _v(inputs.discount_for_lack_of_marketability)
        if dlom:
            eq_lo, eq_mid, eq_hi = eq_lo * (1 - dlom), eq_mid * (1 - dlom), eq_hi * (1 - dlom)
            trace.add("少数股权折价（DLOM）", f"× (1 − {dlom:.1%}) = {eq_lo:,.0f} – {eq_hi:,.0f}")
            notes.append(
                f"已应用 {dlom:.1%} 的少数股权折价。注意这与「少数股东权益」是两回事——"
                f"后者是资产负债表科目（已在上方扣除），前者是无控制权的估值调整。"
            )

    if eq_mid <= 0:
        notes.append("⚠️ 中枢股权价值为负或零：净债务可能已经超过企业价值，标的是资不抵债状态。")

    return ValuationResult(
        method=f"{inputs.metric_name} 乘数法",
        low=eq_lo, mid=eq_mid, high=eq_hi,
        unit=inputs.unit,
        trace=trace,
        assumptions=inputs.all_assumptions(),
        notes=notes,
    )


# --------------------------------------------------------------------------
# SDE —— 中小企业并购的口径（search fund 主场景）
# --------------------------------------------------------------------------

@dataclass
class SdeBuild:
    """从报表净利润倒推 SDE。

    SDE = 净利润 + 利息 + 所得税 + 折旧摊销
               + 所有者超市场水平薪酬
               + 个人性质费用（车、差旅、俱乐部等）
               − 一次性收益 + 一次性损失

    每一项都要来源。这是 SDE 最容易做手脚的地方，也是买方最该较真的地方。
    """

    net_income: Assumption
    interest: Assumption
    taxes: Assumption
    depreciation_amortization: Assumption
    owner_compensation_actual: Assumption
    owner_compensation_market: Assumption
    personal_expenses: Assumption = field(
        default_factory=lambda: Assumption("个人性质费用", 0.0, "万元", "无", Confidence.MEDIUM))
    one_time_gains: Assumption = field(
        default_factory=lambda: Assumption("一次性收益", 0.0, "万元", "无", Confidence.MEDIUM))
    one_time_losses: Assumption = field(
        default_factory=lambda: Assumption("一次性损失", 0.0, "万元", "无", Confidence.MEDIUM))

    def all_assumptions(self) -> list[Assumption]:
        return [self.net_income, self.interest, self.taxes,
                self.depreciation_amortization, self.owner_compensation_actual,
                self.owner_compensation_market, self.personal_expenses,
                self.one_time_gains, self.one_time_losses]


def build_sde(b: SdeBuild) -> tuple[Assumption, Trace, list[Assumption]]:
    """构建 SDE。返回 (SDE 假设对象, 追溯, 全部输入假设)。"""
    trace = Trace()
    ni = _v(b.net_income)
    trace.add("净利润", f"{ni:,.0f}")

    interest = _v(b.interest)
    taxes = _v(b.taxes)
    da = _v(b.depreciation_amortization)
    trace.add("加回利息与税", f"+ {interest:,.0f} + {taxes:,.0f} = +{interest + taxes:,.0f}")
    trace.add("加回折旧摊销", f"+ {da:,.0f}")

    ebitda = ni + interest + taxes + da
    trace.add("= EBITDA", f"{ebitda:,.0f}")

    actual = _v(b.owner_compensation_actual)
    market = _v(b.owner_compensation_market)
    comp_adj = actual - market
    trace.add("所有者薪酬调整", f"实发 {actual:,.0f} − 市场水平 {market:,.0f} = {comp_adj:+,.0f}")

    personal = _v(b.personal_expenses)
    if personal:
        trace.add("加回个人性质费用", f"+ {personal:,.0f}")

    gains = _v(b.one_time_gains)
    losses = _v(b.one_time_losses)
    ote = -gains + losses
    if gains or losses:
        trace.add("一次性项目净调整", f"− {gains:,.0f} + {losses:,.0f} = {ote:+,.0f}")

    sde = ebitda + comp_adj + personal + ote
    trace.add("= SDE", f"{sde:,.0f} 万元")

    conf = min(
        (b.net_income.confidence, b.depreciation_amortization.confidence,
         b.owner_compensation_market.confidence),
        key=lambda c: ["高", "中", "低", "缺失"].index(c.value),
    )

    sde_obj = Assumption(
        "SDE", sde, "万元",
        source=f"由 {b.net_income.source} 等推算",
        confidence=conf,
        note="所有者薪酬按市场水平调整；个人费用与一次性项目已加回",
    )
    return sde_obj, trace, b.all_assumptions()


def run_sde_multiple(
    sde: Assumption,
    multiple_low: Assumption,
    multiple_mid: Assumption,
    multiple_high: Assumption,
    net_debt: Assumption,
    **kwargs,
) -> ValuationResult:
    """SDE 乘数法。中小企业并购的标准做法。"""
    inputs = MultiplesInputs(
        metric_name="SDE",
        metric_value=sde,
        multiple_low=multiple_low,
        multiple_mid=multiple_mid,
        multiple_high=multiple_high,
        net_debt=net_debt,
        **kwargs,
    )
    return run_multiples(inputs)


# --------------------------------------------------------------------------
# 可比公司汇总
# --------------------------------------------------------------------------

def comps_summary(
    peers: list[dict],
    metric_key: str,
    multiplier_key: str,
) -> tuple[dict[str, float], Trace, list[Assumption]]:
    """把一组可比公司汇总成倍数分位数。

    peers 每项形如：
        {"name": "某公司", "ev_ebitda": 10.5, "ebitda": 3150, "source": "SEC EDGAR 2025-12-31"}

    关键：**每个倍数必须带来源和时点**。没有时点的倍数不可比。
    """
    trace = Trace()
    values: list[float] = []
    assumptions: list[Assumption] = []

    for p in peers:
        m = p.get(multiplier_key)
        if m is None or m <= 0:
            trace.add(f"跳过 {p.get('name', '?')}", f"{multiplier_key} 缺失或非正")
            continue
        values.append(float(m))
        assumptions.append(Assumption(
            name=f"{p.get('name', '?')} 的 {multiplier_key}",
            value=float(m),
            unit="x",
            source=p.get("source", "未注明"),
            confidence=Confidence.HIGH if p.get("source") else Confidence.LOW,
        ))

    if len(values) < 3:
        trace.add("样本不足", f"有效可比公司仅 {len(values)} 家，少于 3 家不建议出区间")
        return {}, trace, assumptions

    values.sort()
    n = len(values)

    def q(frac: float) -> float:
        idx = frac * (n - 1)
        lo_i, hi_i = int(idx), min(int(idx) + 1, n - 1)
        return values[lo_i] + (values[hi_i] - values[lo_i]) * (idx - lo_i)

    stats = {
        "n": n,
        "min": values[0],
        "p25": q(0.25),
        "median": q(0.50),
        "p75": q(0.75),
        "max": values[-1],
        "mean": sum(values) / n,
    }
    trace.add("样本数", f"{n} 家可比公司")
    trace.add("分位数", f"P25={stats['p25']:.2f}x  中位={stats['median']:.2f}x  P75={stats['p75']:.2f}x")

    return stats, trace, assumptions
