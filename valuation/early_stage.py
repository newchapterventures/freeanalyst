"""早期项目估值 —— DCF 和乘数法都用不了的那一段。

## 为什么需要单独一套

DCF 要现金流，乘数法要 EBITDA 或收入。pre-revenue 的项目两样都没有。
硬套的结果是：分母是 0 或者负数，算出来的数毫无意义，而且**不会报错**。

早期项目要用的是另一套逻辑 —— 它们估的不是现金流，是**风险和里程碑**。

## 三个方法各自回答什么问题

| 方法 | 回答的问题 | 本质 |
|---|---|---|
| **Berkus** | 去掉了几个风险？ | 每个风险值一点钱，加起来 |
| **Scorecard** | 比市场上的同类项目好还是差？ | 从区域基准出发做加权调整 |
| **VC 法** | 要赚到目标回报，今天最多能付多少？ | 从退出价倒推 |

**三者的哲学完全不同**，所以它们给出不同的数**是正常的**。
如果三个数一样，说明有人在凑数。

## 最重要的一个警告：锚点不能照抄

Berkus 的"每个风险因素最多值 50 万美元"和 Scorecard 的"区域平均 pre-money"，
都是**外部锚点**，不是从标的数据算出来的。

Berkus 那个数字来自 1990 年代的美国西海岸互联网创业潮。**直接搬到现在、
搬到中国，是错的。** 所以这两个锚点在本模块里都是**必须显式给值的参数，
且必须带来源** —— 没有默认值，不给就报错。

## 输出纪律

和主引擎一致：区间、追溯、缺口、低置信度清单，一个不少。
**缺的就说缺，不算出来就报错，不用行业均值填。**
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .core import Assumption, Confidence, Trace, ValuationResult


class EarlyStageError(ValueError):
    """输入不成立。不猜，直接报错。"""


# ---------------------------------------------------------------------------
# 一个带不确定性的数
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Range:
    """low ≤ mid ≤ high。三个数都给，因为估值本来就该给区间。

    如果只有一个确定的值，用 `Range.point(v)` —— 那是"这个因素我不给区间"，
    和"这个因素我算出来是零宽"是两回事，前者会进缺口清单。
    """

    low: float
    mid: float
    high: float

    @classmethod
    def point(cls, v: float) -> Range:
        return cls(v, v, v)

    def __post_init__(self) -> None:
        if not (self.low <= self.mid <= self.high):
            raise EarlyStageError(
                f"区间必须满足 low ≤ mid ≤ high，收到 ({self.low}, {self.mid}, {self.high})"
            )

    @property
    def is_point(self) -> bool:
        return self.low == self.mid == self.high

    def scaled(self, k: float) -> Range:
        return Range(self.low * k, self.mid * k, self.high * k)

    def __str__(self) -> str:
        if self.is_point:
            return _fmt(self.mid)
        return f"{_fmt(self.low)} / {_fmt(self.mid)} / {_fmt(self.high)}"


def _fmt(v: float) -> str:
    """按数量级选格式。

    **不能用固定的 :,.0f** —— 达成度是 0–1 的小数，四舍五入会把
    0.7 / 0.8 / 0.9 全显示成 "1"，追溯里的数字就和输入对不上了。
    追溯对不上比算错更糟：它让人没法核对。
    """
    if v == 0:
        return "0"
    if abs(v) < 10:
        s = f"{v:,.3f}".rstrip("0").rstrip(".")
        return s or "0"
    return f"{v:,.0f}"


@dataclass
class Factor:
    """一个评分因素。

    value 的口径由各方法定义：
      - Berkus：0–1 的达成度（这个风险去掉了多少）
      - Scorecard：相对市场平均的倍率，通常 0.5–1.5（1.0 = 与平均持平）
    """

    name: str
    value: Range
    source: str = "未注明"
    confidence: Confidence = Confidence.LOW
    weight: float | None = None
    note: str = ""

    def to_assumption(self, unit: str = "") -> Assumption:
        return Assumption(
            name=self.name,
            value=self.value.mid,
            unit=unit,
            source=self.source,
            confidence=self.confidence,
            note=self.note or (f"区间 {self.value}" if not self.value.is_point else ""),
        )


# ---------------------------------------------------------------------------
# 1. Berkus 法
# ---------------------------------------------------------------------------

# 五个风险因素。Berkus 的原始定义，顺序就是我列的这个。
BERKUS_FACTORS = (
    "基本价值（点子/商业计划成立）",
    "技术风险（做出原型）",
    "执行风险（团队到位）",
    "市场风险（战略关系/客户意向）",
    "生产风险（产品上市并产生销售）",
)


def berkus(
    factors: list[Factor],
    per_factor_cap: Range,
    cap_source: str = "未注明",
    cap_confidence: Confidence = Confidence.LOW,
    revenue_component: Assumption | None = None,
    unit: str = "万元",
) -> ValuationResult:
    """Berkus 法：五个风险因素，每个最多值 `per_factor_cap`。

    ## 这个方法的适用边界（Berkus 本人的定义）

    - **pre-revenue** 专用。已经有稳定收入的项目请用乘数法或 DCF。
    - 结果通常落在很小一个量级（原版上限 2–2.5 百万美元）。
      **它给不出一个亿的估值，硬拉出来就是自欺欺人。**
    - 是对"去掉了多少风险"的粗略打分，不是精细模型。

    ## `per_factor_cap` 为什么必须显式给

    原版是 50 万美元／因素（1990 年代，美国西海岸）。
    **照抄到别的年份、别的地区是错的。** 所以这里没有默认值，
    而且要求注明来源 —— 你填的这个数，就是你的估值锚，
    它比后面所有计算加起来都重要。

    `revenue_component`：如果已经有一点收入，可以额外加一块。
    原版 Berkus 不含这一项，这里是扩展，会明确标出来。
    """
    if not factors:
        raise EarlyStageError("至少要给一个风险因素")
    if per_factor_cap.low <= 0:
        raise EarlyStageError(f"每个因素的封顶值必须为正，收到 {per_factor_cap}")

    trace = Trace()
    assumptions: list[Assumption] = []
    notes: list[str] = []

    cap_a = Assumption(
        name="每因素封顶值", value=per_factor_cap.mid, unit=unit,
        source=cap_source, confidence=cap_confidence,
        note=f"区间 {per_factor_cap}" if not per_factor_cap.is_point else "",
    )
    assumptions.append(cap_a)

    if cap_confidence is Confidence.MISSING:
        notes.append("封顶值没有来源 —— 这是整个结果里权重最大、最不确定的一个数")

    trace.add("封顶值", f"{per_factor_cap} {unit}/因素（来源：{cap_source}）")
    trace.add("因素数", str(len(factors)))

    count = 0
    for f in factors:
        if not (0.0 <= f.value.low and f.value.high <= 1.0):
            raise EarlyStageError(
                f"Berkus 的达成度必须在 0–1 之间，{f.name} 收到 {f.value}"
            )
        assumptions.append(f.to_assumption())
        trace.add(f"  {f.name[:20]}", f"{f.value}  ×  {per_factor_cap}")
        count += 1

    low = sum(f.value.low for f in factors) * per_factor_cap.low
    mid = sum(f.value.mid for f in factors) * per_factor_cap.mid
    high = sum(f.value.high for f in factors) * per_factor_cap.high

    trace.add("小计", f"{low:,.0f} – {high:,.0f} {unit}")

    if revenue_component is not None:
        assumptions.append(revenue_component)
        if revenue_component.is_missing:
            notes.append(f"收入加项缺失（{revenue_component.name}），未计入")
        else:
            rv = revenue_component.value or 0.0
            low += rv
            mid += rv
            high += rv
            trace.add("收入加项", f"+ {rv:,.0f} {unit}（{revenue_component.source}）")
            notes.append(
                "收入加项是原版 Berkus 之外的扩展，原版只适用于 pre-revenue。"
                "已经有一定收入的项目，乘数法比这个靠谱。"
            )

    if count < len(BERKUS_FACTORS):
        missing_names = [n for n in BERKUS_FACTORS
                         if not any(n[:6] in f.name for f in factors)]
        if missing_names:
            notes.append(f"未评估的风险因素：{'、'.join(missing_names)}")

    return ValuationResult(
        method=f"Berkus 法（{count} 个风险因素）",
        low=low, mid=mid, high=high, unit=unit,
        trace=trace, assumptions=assumptions, notes=notes,
    )


# ---------------------------------------------------------------------------
# 2. Scorecard 法（Bill Payne）
# ---------------------------------------------------------------------------

# 标准权重。这是一套行业惯例，不是从标的数据推出来的 —— 所以它是可改的，
# 改的时候要意识到自己在改什么。
SCORECARD_WEIGHTS: dict[str, float] = {
    "管理团队实力": 0.30,
    "机会规模": 0.25,
    "产品/技术": 0.15,
    "竞争环境": 0.10,
    "营销渠道/合作关系": 0.10,
    "后续融资需求": 0.05,
    "其他": 0.05,
}

SCORECARD_MIN = 0.5    # 相对市场平均最差
SCORECARD_MAX = 1.5    # 相对市场平均最好


def scorecard(
    base: Range,
    base_source: str,
    factors: dict[str, Factor] | list[Factor],
    base_confidence: Confidence = Confidence.LOW,
    unit: str = "万元",
) -> ValuationResult:
    """Scorecard 法：从区域基准出发，按加权因素调整。

    ## 公式

        pre-money = 基准 × Σ(权重 × 倍率)

    倍率以 1.0 为"与市场平均持平"，标准范围 0.5–1.5。
    所有倍率都取 1.0 时，结果就等于基准 —— 这是这个方法该有的性质。

    ## `base` 为什么必须显式给

    「本地区、本阶段、本行业的平均 pre-money」是一个**外部事实**，
    不是能从标的材料里算出来的数。没有它这个方法无从谈起。

    而且这个数**时效性极强**：融资市场冷热差一年，同一个项目的定价能差一半。
    所以必须注明来源和基准日 —— 拿三年前的平均值来套今天的项目，
    是这套方法最常见的误用。

    ## 权重

    默认用 Bill Payne 的标准权重（见 `SCORECARD_WEIGHTS`）。
    传 `dict` 时用默认权重；传 `list[Factor]` 时用每个 Factor 自带的 `weight`。
    """
    trace = Trace()
    assumptions: list[Assumption] = []
    notes: list[str] = []

    base_a = Assumption(
        name="区域基准 pre-money", value=base.mid, unit=unit,
        source=base_source, confidence=base_confidence,
        note=f"区间 {base}" if not base.is_point else "",
    )
    assumptions.append(base_a)
    trace.add("基准", f"{base} {unit}（来源：{base_source}）")
    if base_confidence is Confidence.MISSING:
        notes.append("基准值没有来源 —— 这个方法的结果几乎完全由它决定")
    if base.is_point:
        notes.append(f"基准值给的是单点 {base.mid:,.0f}，没有区间 —— "
                     f"实际市场平均总有波动，建议给区间")

    # 归一化成 (name, Range, weight, source, confidence) 列表
    items: list[tuple[str, Range, float, str, Confidence]] = []
    if isinstance(factors, dict):
        weights = dict(SCORECARD_WEIGHTS)
        for name, f in factors.items():
            if not isinstance(f, Factor):
                raise EarlyStageError(
                    f"因素 {name!r} 必须是 Factor 对象（要带来源），收到 "
                    f"{type(f).__name__}。传裸 Range 会让这个评分失去出处 —— "
                    f"每个评分都必须能回答「这个分是谁给的、凭什么」。"
                )
            w = weights.pop(name, None)
            if w is None:
                raise EarlyStageError(
                    f"不认识的评分因素 {name!r}。标准因素：{'、'.join(SCORECARD_WEIGHTS)}"
                )
            items.append((name, f.value, w, f.source, f.confidence))
        if weights:
            # 只评估了部分因素 → 把已有因素的权重按比例放大到 1.0
            provided = 1.0 - sum(weights.values())
            notes.append(
                f"以下标准因素未提供，其权重（合计 {sum(weights.values()):.0%}）"
                f"已按比例归一化 —— 已有因素权重放大了 {1 / provided:.2f} 倍："
                f"{'、'.join(weights)}"
            )
            items = [(n, r, w / provided, s, c) for n, r, w, s, c in items]
    else:
        for f in factors:
            if f.weight is None:
                raise EarlyStageError(f"因素 {f.name!r} 没给权重")
            items.append((f.name, f.value, f.weight, f.source, f.confidence))

    total_w = sum(w for _, _, w, _, _ in items)
    if abs(total_w - 1.0) > 0.001:
        raise EarlyStageError(
            f"权重合计必须为 1.0，收到 {total_w:.4f}"
            f"（{'传 dict 时未提供的因素会按比例归一化' if isinstance(factors, dict) else '传 list 时请自己配平'}）"
        )

    for name, rng, w, src, conf in items:
        if not (SCORECARD_MIN <= rng.low and rng.high <= SCORECARD_MAX):
            raise EarlyStageError(
                f"{name} 的倍率必须在 {SCORECARD_MIN}–{SCORECARD_MAX} 之间，收到 {rng}"
            )
        assumptions.append(Assumption(
            name=name, value=rng.mid, unit="倍率", source=src,
            confidence=conf, note=f"权重 {w:.0%}，区间 {rng}",
        ))
        trace.add(f"  {name[:12]}", f"{rng}  × 权重 {w:.0%}")

    adj = sum(r.mid * w for _, r, w, _, _ in items)
    low_f = sum(r.low * w for _, r, w, _, _ in items)
    high_f = sum(r.high * w for _, r, w, _, _ in items)

    trace.add("加权倍率", f"{low_f:.3f} / {adj:.3f} / {high_f:.3f}")
    trace.add("基准 × 倍率", f"{base} × {adj:.3f}")

    low = base.low * low_f
    mid = base.mid * adj
    high = base.high * high_f

    dev = adj - 1.0
    if abs(dev) < 1e-9:
        notes.append(f"与区域基准持平（{base.mid:,.0f} {unit}）")
    else:
        notes.append(
            f"相对区域基准{'高' if dev > 0 else '低'} {abs(dev):.1%}"
            f"（对照基准 {base.mid:,.0f} {unit}）"
        )

    return ValuationResult(
        method="Scorecard 法",
        low=low, mid=mid, high=high, unit=unit,
        trace=trace, assumptions=assumptions, notes=notes,
    )


# ---------------------------------------------------------------------------
# 3. VC 法
# ---------------------------------------------------------------------------

def vc_method(
    exit_value: Assumption,
    investment: Assumption,
    years_to_exit: Assumption,
    target_multiple: Assumption | None = None,
    target_irr: Assumption | None = None,
    future_dilution: Assumption | None = None,
    unit: str = "万元",
) -> ValuationResult:
    """VC 法：从退出价倒推今天该付多少。

    ## 公式

        own_exit  = 目标倍数 × 投资额 / 退出价值      ← 退出时要占多少
        own_today = own_exit / (1 − 未来稀释)          ← 今天要占多少
        post      = 投资额 / own_today
        pre       = post − 投资额

    ## 稀释这一项最容易漏，而且漏了会让估值虚高

    今天占 20%，不代表退出那天还占 20%。后续每一轮都会稀释。
    忽略稀释的话，算出来的 pre-money 会**偏高**，偏高幅度正好等于被稀释掉的那部分。

    稀释是**必然发生**的（只要还要再融资），不是悲观假设。真正的未知只是比例。

    ## target_multiple 和 target_irr 二选一

        目标倍数 M 与目标 IRR r 的关系：M = (1 + r) ^ 年数

    两个都给的话会交叉验证，不一致就报出来 —— 这通常说明有一个数拍错了。

    ## 这个方法的性质要说清楚

    它算出来的不是"公司值多少"，而是**"按这个目标回报，今天最多能付多少"**。
    目标倍数定得高，算出来的 pre-money 就低。所以它天然偏保守，
    而且**结果的合理性完全取决于目标倍数定得对不对**。
    """
    if target_multiple is None and target_irr is None:
        raise EarlyStageError("目标倍数和目标 IRR 至少要给一个")

    for a in (exit_value, investment, years_to_exit):
        if a.is_missing:
            raise EarlyStageError(f"{a.name} 缺失，VC 法算不了（不猜）")
    ev = exit_value.value or 0.0
    inv = investment.value or 0.0
    yrs = years_to_exit.value or 0.0
    if ev <= 0:
        raise EarlyStageError(f"退出价值必须为正，收到 {ev}")
    if inv <= 0:
        raise EarlyStageError(f"投资额必须为正，收到 {inv}")
    if yrs <= 0:
        raise EarlyStageError(f"退出年限必须为正，收到 {yrs}")

    trace = Trace()
    assumptions = [exit_value, investment, years_to_exit]
    notes: list[str] = []

    # 目标倍数：直接给，或从 IRR 推
    if target_multiple is not None and not target_multiple.is_missing:
        m = target_multiple.value or 0.0
        assumptions.append(target_multiple)
        trace.add("目标倍数", f"{m:.1f}x（来源：{target_multiple.source}）")
        if target_irr is not None and not target_irr.is_missing:
            r = target_irr.value or 0.0
            assumptions.append(target_irr)
            m_implied = (1 + r) ** yrs
            trace.add("IRR 折算", f"(1 + {r:.1%}) ^ {yrs:.0f} = {m_implied:.2f}x")
            if abs(m_implied - m) / m > 0.05:
                notes.append(
                    f"目标倍数 {m:.1f}x 与目标 IRR {r:.0%}/{yrs:.0f} 年不一致："
                    f"{r:.0%} 对应的倍数是 {m_implied:.2f}x，差 {abs(m_implied - m) / m:.0%}。"
                    f"两个数有一个拍错了 —— 先定清楚是哪个。"
                )
    else:
        assert target_irr is not None
        r = target_irr.value or 0.0
        assumptions.append(target_irr)
        m = (1 + r) ** yrs
        trace.add("目标倍数", f"由 IRR 折算：(1 + {r:.1%}) ^ {yrs:.0f} = {m:.2f}x")

    if m <= 1.0:
        raise EarlyStageError(f"目标倍数必须大于 1，收到 {m:.2f}x")

    # 稀释
    d = 0.0
    if future_dilution is not None:
        assumptions.append(future_dilution)
        if future_dilution.is_missing:
            notes.append(
                "未来稀释比例缺失。这里按 0 计算 —— **这是乐观假设，会让结果偏高**。"
                "只要还要再融资，稀释就会发生。"
            )
        else:
            d = future_dilution.value or 0.0
    else:
        notes.append(
            "没有提供未来稀释比例，按 0 计算 —— **这是乐观假设**。"
            "建议补上：早期项目后续通常还有 1–3 轮。"
        )

    if not (0.0 <= d < 1.0):
        raise EarlyStageError(f"稀释比例必须在 [0, 1) 之间，收到 {d}")

    own_exit = m * inv / ev
    trace.add("退出时所需持股", f"{m:.2f} × {inv:,.0f} / {ev:,.0f} = {own_exit:.2%}")

    if own_exit >= 1.0:
        notes.append(
            f"退出时要占 {own_exit:.0%} —— 超过 100%，说明按这个目标倍数和退出价，"
            f"这笔投资根本达不到要求。要么退出假设太保守，要么目标倍数太高。"
        )

    own_today = own_exit / (1 - d) if d < 1 else float("inf")
    if d > 0:
        trace.add("今天所需持股", f"{own_exit:.2%} / (1 − {d:.1%}) = {own_today:.2%}")

    post = inv / own_today if own_today > 0 else float("inf")
    pre = post - inv
    trace.add("投后", f"{inv:,.0f} / {own_today:.2%} = {post:,.0f}")
    trace.add("投前", f"{post:,.0f} − {inv:,.0f} = {pre:,.0f}")

    # 区间：目标倍数和退出价的粗糙上下浮动会误导，所以这里不动边界，
    # 而是明确说明区间该由什么产生。
    notes.append(
        "VC 法给的是单点。**它的区间应该由退出价和目标倍数的情景来产生，"
        "不是在这里拍一个 ±20%。** 用 first_chicago() 跑多情景。"
    )

    return ValuationResult(
        method=f"VC 法（目标 {m:.1f}x / {yrs:.0f} 年）",
        low=pre, mid=pre, high=pre, unit=unit,
        trace=trace, assumptions=assumptions, notes=notes,
    )


def vc_required_exit(
    pre_money: Assumption,
    investment: Assumption,
    years_to_exit: Assumption,
    target_multiple: Assumption,
    future_dilution: Assumption | None = None,
    unit: str = "万元",
) -> ValuationResult:
    """反向 VC 法：按对方给的价格，要涨到多大才能达到目标回报。

    **这是 §6.6 要求的反向估值在早期项目上的形态。**

    卖方报一个 pre-money，别急着说贵或便宜 —— 先把它翻译成
    "要实现目标回报，退出时得值多少钱"，再看那个数是否可信。
    一个 8 亿的退出假设，比一句"估值偏高"有用得多。
    """
    for a in (pre_money, investment, years_to_exit, target_multiple):
        if a.is_missing:
            raise EarlyStageError(f"{a.name} 缺失，反向 VC 法算不了")

    pre = pre_money.value or 0.0
    inv = investment.value or 0.0
    yrs = years_to_exit.value or 0.0
    m = target_multiple.value or 0.0

    if pre < 0 or inv <= 0 or yrs <= 0 or m <= 1:
        raise EarlyStageError(
            f"输入不成立：pre-money={pre}, 投资额={inv}, 年限={yrs}, 目标倍数={m}"
        )

    trace = Trace()
    assumptions = [pre_money, investment, years_to_exit, target_multiple]
    notes: list[str] = []

    post = pre + inv
    own_today = inv / post
    trace.add("投后", f"{pre:,.0f} + {inv:,.0f} = {post:,.0f}")
    trace.add("本轮持股", f"{inv:,.0f} / {post:,.0f} = {own_today:.2%}")

    d = 0.0
    if future_dilution is not None and not future_dilution.is_missing:
        assumptions.append(future_dilution)
        d = future_dilution.value or 0.0
    else:
        notes.append("未提供未来稀释比例，按 0 计算 —— 所需退出价会偏低")
    own_exit = own_today * (1 - d)
    if d > 0:
        trace.add("退出时持股", f"{own_today:.2%} × (1 − {d:.1%}) = {own_exit:.2%}")

    required_exit = m * inv / own_exit if own_exit > 0 else float("inf")
    trace.add("所需退出价值", f"{m:.2f} × {inv:,.0f} / {own_exit:.2%} = {required_exit:,.0f}")

    implied_cagr = (required_exit / post) ** (1 / yrs) - 1 if post > 0 else float("inf")
    trace.add("公司需实现年化", f"({required_exit:,.0f} / {post:,.0f}) ^ (1/{yrs:.0f}) − 1 = {implied_cagr:.1%}")

    notes.append(
        f"按这个价格，公司要在 {yrs:.0f} 年内做到 {required_exit:,.0f} {unit} 的退出价值，"
        f"即从投后 {post:,.0f} 起算年化 {(implied_cagr):.1%}。"
        f"**谈论这个数是否可信，比谈论估值高低有用得多。**"
    )

    return ValuationResult(
        method=f"反向 VC 法（{m:.1f}x / {yrs:.0f} 年）",
        low=required_exit, mid=required_exit, high=required_exit, unit=unit,
        trace=trace, assumptions=assumptions, notes=notes,
        # **不是估值**：这个数回答的是"需要多大的退出"，
        # 不是"公司值多少"。放进多方法对照会污染差额。
        is_valuation=False,
    )


# ---------------------------------------------------------------------------
# 4. First Chicago —— 给区间用的
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    """一个情景：概率 + 该情景下的估值。"""

    label: str
    probability: float          # 0–1
    value: float
    basis: str = ""             # 这个情景的估值是怎么来的


def first_chicago(
    outcomes: list[Outcome],
    unit: str = "万元",
    note: str = "",
) -> ValuationResult:
    """First Chicago 法：分情景，按概率加权。

    ## 为什么早期项目特别需要这个

    pre-revenue 项目的价值分布**不是钟形，是一头沉**：
    大概率归零，小概率翻很多倍。用单一情景的均值去描述它，
    会得到一个"看起来正常"但完全不对的数。

    所以早期项目该问的不是"它值多少"，而是
        「成功的话值多少？成功的概率有多大？失败的话剩多少？」

    ## 加权结果不等于你的出价

    期望值（概率加权）是**定价的输入之一，不是定价本身**。
    早期项目的期望值通常远低于成功情景 —— 如果有人只拿期望值来谈，
    说明他要么不懂这个阶段的风险，要么在刻意压价。
    """
    if not outcomes:
        raise EarlyStageError("至少要给一个情景")

    total_p = sum(o.probability for o in outcomes)
    if abs(total_p - 1.0) > 0.001:
        raise EarlyStageError(
            f"情景概率合计必须为 1.0，收到 {total_p:.4f}。"
            f"**不要自动归一化** —— 概率加起来不是 1，说明你漏了一个情景。"
        )
    for o in outcomes:
        if not (0.0 <= o.probability <= 1.0):
            raise EarlyStageError(f"情景 {o.label} 的概率 {o.probability} 不在 [0,1] 内")
        if o.value < 0:
            raise EarlyStageError(f"情景 {o.label} 的估值不能为负")

    trace = Trace()
    notes: list[str] = [note] if note else []

    weighted = 0.0
    for o in outcomes:
        trace.add(f"  {o.label[:16]}", f"{o.probability:.0%} × {o.value:,.0f} = {o.probability * o.value:,.0f}")
        weighted += o.probability * o.value

    trace.add("概率加权", f"{weighted:,.0f} {unit}")

    values = sorted((o.value, o.label) for o in outcomes)
    low, high = values[0][0], values[-1][0]

    # 各情景本身就是假设。概率是这个方法里最关键的输入，
    # 所以逐条记进 assumptions，让它们出现在低置信度清单里。
    assumptions = [
        Assumption(
            name=f"情景「{o.label}」",
            value=o.probability,
            unit="概率",
            source=o.basis or "未注明",
            confidence=Confidence.LOW if not o.basis else Confidence.MEDIUM,
            note=f"该情景下的估值 {o.value:,.0f} {unit}",
        )
        for o in outcomes
    ]

    success = [o for o in outcomes if o.label.startswith("成功") or o.label.startswith("乐观")]
    if success:
        s = success[0]
        notes.append(
            f"成功情景单独看是 {s.value:,.0f} {unit}（概率 {s.probability:.0%}）—— "
            f"概率加权后是 {weighted:,.0f}。**两个数都要看**："
            f"加权值用于定价，成功情景用于判断「要相信什么才能赚到钱」。"
        )

    if low == 0:
        notes.append(
            "含归零情景。pre-revenue 项目的价值分布是一头沉的，"
            "不是钟形的 —— 均值和中位数在这里都会误导。"
        )

    return ValuationResult(
        method=f"First Chicago（{len(outcomes)} 情景）",
        low=low, mid=weighted, high=high, unit=unit,
        trace=trace, assumptions=assumptions, notes=notes,
    )


# ---------------------------------------------------------------------------
# 多方法交叉
# ---------------------------------------------------------------------------

def compare(results: list[ValuationResult], unit: str = "万元") -> str:
    """多方法交叉对照，**差额必须报出来**（§6.7）。

    早期项目用不同方法得出来的数**本来就会差很多**，因为它们的哲学不同。
    这不算失败，算信息 —— 差额大说明定价高度依赖你选了哪套逻辑，
    这本身就是该写进备忘录的结论。

    **反向估值不参与对照**（`is_valuation=False`）—— 它输出的是
    "需要多大的退出"，不是"公司值多少"。混进来算差额会得出一个
    毫无意义的数，而且看起来还挺像回事。
    """
    if not results:
        return "（没有可对照的结果）"

    vals = [r for r in results if r.is_valuation]
    others = [r for r in results if not r.is_valuation]

    lines: list[str] = []
    if vals:
        lines.append(f"{'方法':<28}{'低':>12}{'中枢':>12}{'高':>12}")
        lines.append("-" * 64)
        for r in vals:
            lines.append(f"{r.method[:26]:<28}{r.low:>12,.0f}{r.mid:>12,.0f}{r.high:>12,.0f}")

    mids = [r.mid for r in vals if r.mid > 0]
    if len(mids) >= 2:
        lo, hi = min(mids), max(mids)
        spread = (hi - lo) / ((hi + lo) / 2)
        lines.append("")
        lines.append(f"中枢差额 {spread:.0%}（{lo:,.0f} – {hi:,.0f} {unit}）")
        if spread > 0.5:
            lines.append(
                "**差额超过 50%：定价高度依赖方法选择，单点结论不可靠。**"
                "谈判必须给区间，并且说清用的是哪套逻辑。"
            )
        lines.append(
            "差额大不代表哪个方法错了 —— Berkus 估的是「去掉了多少风险」，"
            "First Chicago 估的是「概率加权的期望值」，VC 法回答的是"
            "「按目标回报今天最多能付多少」。**三个问题不同，答案自然不同。**"
        )

    if others:
        lines.append("")
        lines.append("（以下不属于估值，未计入差额）")
        for r in others:
            lines.append(
                f"  {r.method}：{r.mid:,.0f} {unit} "
                f"—— 回答的是「需要多大的退出」，不是「值多少」"
            )

    return "\n".join(lines) if lines else "（没有可对照的结果）"
