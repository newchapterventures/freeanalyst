"""估值引擎的基础对象。

三条纪律（这个模块存在的原因）：

1. **纯代码，不用 LLM 算。** DCF 和 Comps 是确定性数学。让大模型去算，
   会算错，而且算错了你查不出来。模型只负责"把材料抽成数字"和"给假设找依据"。

2. **每个假设必须带来源。** 一个没有来源的假设是隐性炸弹 —— 它不报错，
   但会让整个结果偏掉，而且你事后无从判断错在哪。

3. **缺数据就说缺。** 禁止用行业均值之类的默认值把洞填掉。
   缺口必须作为输出的一部分交回去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Confidence(str, Enum):
    """假设的置信度。来源不同，可信程度不同。"""

    HIGH = "高"        # 审计报告、已签合同、官方披露
    MEDIUM = "中"      # 管理报表、可交叉验证的访谈
    LOW = "低"         # 管理层口径、规划目标、行业传闻
    MISSING = "缺失"    # 没有这个数

    def __str__(self) -> str:  # pragma: no cover - 仅用于展示
        return self.value


class Purpose(str, Enum):
    """估值目的。决定方法、口径和立场取向。"""

    FINANCING = "融资定价"
    MNA = "并购定价"
    NAV = "投后NAV"
    COMPLIANCE = "税务合规"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


class Stance(str, Enum):
    """立场。买方保守、卖方激进，同一个模型取向相反。"""

    BUYER = "买方"
    SELLER = "卖方"
    NEUTRAL = "中立"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


class Stage(str, Enum):
    """标的阶段。决定用哪套方法。"""

    EARLY = "早期项目"          # 无收入或有收入无利润
    GROWTH = "成长企业"         # 有收入、EBITDA 为正但薄
    MATURE = "成熟企业"         # 稳定 EBITDA
    OWNER_OPERATED = "业主经营"  # 中小企业，SDE 口径

    def __str__(self) -> str:  # pragma: no cover
        return self.value


@dataclass(frozen=True)
class Assumption:
    """一个估值假设：值 + 来源 + 置信度。

    这是整个引擎最重要的对象。任何进入计算的数字都必须包成 Assumption，
    否则无从追溯。
    """

    name: str
    value: float | None
    unit: str = ""
    source: str = "未注明"
    confidence: Confidence = Confidence.MEDIUM
    note: str = ""

    @property
    def is_missing(self) -> bool:
        return self.value is None or self.confidence is Confidence.MISSING

    def __repr__(self) -> str:  # pragma: no cover
        if self.is_missing:
            return f"Assumption({self.name}=缺失)"
        return f"Assumption({self.name}={self.value}{self.unit} 来源={self.source} 置信={self.confidence})"


@dataclass
class Trace:
    """计算过程的追溯记录。

    每做一步运算就记一条，最后原样输出。用户能看到"这个数字怎么来的"。
    """

    steps: list[tuple[str, str]] = field(default_factory=list)

    def add(self, label: str, expression: str) -> None:
        self.steps.append((label, expression))

    def render(self) -> str:
        if not self.steps:
            return "（无计算步骤）"
        width = max(len(label) for label, _ in self.steps)
        return "\n".join(f"  {label:<{width}}  {expr}" for label, expr in self.steps)


@dataclass
class Scenario:
    """Step 0 —— 场景声明。

    这五项不定，方法无从选，结果也无意义。
    """

    purpose: Purpose
    stance: Stance
    stage: Stage
    valuation_date: str
    currency: str
    equity_scope: str = "100%"

    def describe(self) -> str:
        return (
            f"{self.stage} · {self.purpose} · {self.stance}立场 · "
            f"{self.equity_scope}权益 · 基准日 {self.valuation_date} · {self.currency}"
        )


@dataclass
class ValuationResult:
    """一个估值结果。带完整追溯。"""

    method: str
    low: float
    mid: float
    high: float
    unit: str
    trace: Trace
    assumptions: list[Assumption] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def range_text(self) -> str:
        return f"{self.low:,.0f} – {self.high:,.0f} {self.unit}（中枢 {self.mid:,.0f}）"

    def gaps(self) -> list[Assumption]:
        """本次计算缺失的假设。必须交回给用户。"""
        return [a for a in self.assumptions if a.is_missing]

    def low_confidence(self) -> list[Assumption]:
        """低置信度假设。这些是结果的软肋。"""
        return [a for a in self.assumptions if a.confidence is Confidence.LOW]
