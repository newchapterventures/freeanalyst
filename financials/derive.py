"""从三张表推出估值引擎要的口径。

## 为什么要单独一层

估值引擎要的不是"一张表"，是**几个特定口径的数字**：

    净债务 = 有息负债 − 现金        （DCF 里从企业价值倒推股权价值）
    少数股东权益                     （同样要扣掉）
    非经营性资产                     （要加回）

中间隔着「表里有哪些科目」和「引擎要哪些字段」这道缝。
这个模块就是那道缝。

## 两条纪律

**① 算不出来就报出来，不给默认值。**

净债务算不准（比如只找到短期借款、没找到长期借款），
给一个"看起来合理"的数比报缺更危险 —— 估值会照常跑完，结果是错的。

**② 每个字段都带来源行。**

「净债务 12,345」没人能核对；「短期借款 4,100 + 长期借款 2,300 − 货币资金 3,180」
才能核对。追溯是这个产品的底线。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import Field

#: 哪些算「有息负债」
DEBT_FIELDS: tuple[Field, ...] = (Field.SHORT_TERM_DEBT, Field.LONG_TERM_DEBT)

#: 哪些算「现金及现金等价物」
CASH_FIELDS: tuple[Field, ...] = (Field.CASH, Field.SHORT_TERM_INVESTMENTS)


@dataclass
class Derived:
    """一个推算出来的口径。"""

    name: str
    value: float | None
    unit: str = ""
    #: 构成明细：[(科目, 金额)] —— 让人能核对，不是给一个孤零零的数
    parts: list[tuple[str, float]] = field(default_factory=list)
    missing: list[Field] = field(default_factory=list)
    note: str = ""

    def render(self) -> str:
        if self.value is None:
            names = "、".join(f.value for f in self.missing) or "（无）"
            tail = f"  ← {self.note}" if self.note else ""
            return f"{self.name}：**无法计算** —— 缺 {names}{tail}"
        body = " ".join(
            (f"{v:,.0f}({n})" if i == 0 else
             (f"− {abs(v):,.0f}({n})" if v < 0 else f"+ {v:,.0f}({n})"))
            for i, (n, v) in enumerate(self.parts)
        )
        tail = f"  ← {self.note}" if self.note else ""
        return f"{self.name} = {self.value:,.0f}{self.unit}    {body}{tail}"

    def as_assumption(self, source: str = "由三张表推算", confidence: str = "中"):
        """转成估值引擎的 `Assumption`。

        **推算出来的口径置信度标「中」**：它依赖科目映射全部正确，
        而映射是有可能漏科目的（漏了就是错的，看不出来）。
        勾稽平了才敢标「高」。
        """
        from valuation.core import Assumption, Confidence

        conf = {"高": Confidence.HIGH, "中": Confidence.MEDIUM,
                "低": Confidence.LOW}[confidence]
        return Assumption(self.name, self.value, self.unit, source, conf)


#: 非有息负债科目 —— 用来做「余量」判断
_NON_DEBT_LIABILITIES: tuple[Field, ...] = (
    Field.ACCOUNTS_PAYABLE, Field.DEFERRED_REVENUE,
    Field.ACCRUED_LIABILITIES, Field.TAXES_PAYABLE,
    Field.OTHER_CURRENT_LIABILITIES, Field.OTHER_NONCURRENT_LIABILITIES,
    Field.MINORITY_INTEREST,
)


def net_debt(bal: dict[Field, float]) -> Derived:
    """净债务 = 有息负债 − 现金。

    ## 这是 DCF 里最容易被糊弄过去的一步

    企业价值 EV 是**整个公司**的价值，要得到股权价值必须减掉净债务。
    减错的后果是股权价值同额偏差 —— 而 DCF 的输出看起来完全正常。

    ## 三种情形要分开

    **① 借款科目都在** → 直接算。

    **② 找不到借款科目，但公司本来就没借款** → 算出 0，不该报缺。
       用勾稽关系判定：负债合计减去已识别的非有息负债科目，
       **余量若为 0，就没有地方放借款。**
       实测（Fitbit FY2016）：负债合计 821,694，已识别科目
       （应付账款 + 预收 + 其他流动 + 其他非流动）加起来正好 821,694。

    **③ 只找到部分借款科目**（有短期、没长期）→ 报缺。
       按 0 处理会让净债务偏低、股权价值偏高 —— **往看起来更好的方向偏。**
    """
    debt_parts: list[tuple[str, float]] = []
    cash_parts: list[tuple[str, float]] = []
    missing: list[Field] = []
    note = ""

    for f in DEBT_FIELDS:
        v = bal.get(f)
        if v is None:
            missing.append(f)
        elif v:
            debt_parts.append((f.value, v))

    for f in CASH_FIELDS:
        v = bal.get(f)
        if v is not None and v:
            cash_parts.append((f.value, v))

    if not debt_parts and not cash_parts:
        return Derived("净债务", None, missing=[*DEBT_FIELDS, *CASH_FIELDS])

    if missing:
        # 尝试用「余量」判定是不是真的没有借款
        total_liab = bal.get(Field.TOTAL_LIABILITIES)
        known = sum(bal.get(f, 0.0) or 0.0 for f in _NON_DEBT_LIABILITIES)
        if total_liab is not None and abs(total_liab - known) <= max(abs(total_liab) * 1e-6, 1.0):
            debt_parts = []
            missing = []
            note = ("负债合计与已识别的非有息负债科目完全一致，"
                    "反推有息负债为 0")
        else:
            return Derived(
                "净债务", None, missing=missing,
                note="只找到部分借款科目；漏掉的那部分会让净债务偏低、"
                     "股权价值偏高（往看起来更好的方向偏）",
            )

    if not debt_parts and not cash_parts:
        return Derived("净债务", None, missing=[*DEBT_FIELDS, *CASH_FIELDS])

    debt = sum(v for _, v in debt_parts)
    cash = sum(v for _, v in cash_parts)
    parts = [(n, v) for n, v in debt_parts] + [(n, -v) for n, v in cash_parts]
    return Derived("净债务", debt - cash, parts=parts, note=note)


def minority_interest(bal: dict[Field, float]) -> Derived:
    """少数股东权益。没有这一科目时按 0 处理是**安全**的 ——
    它本来就不是每家公司都有，且为 0 不会造成方向性偏差。"""
    v = bal.get(Field.MINORITY_INTEREST)
    if v is None:
        return Derived("少数股东权益", 0.0, note="报表无此科目，按 0 处理")
    return Derived("少数股东权益", v, parts=[(Field.MINORITY_INTEREST.value, v)])


def ebitda(is_: dict[Field, float]) -> Derived:
    """EBITDA = 营业利润 + 折旧摊销。

    **不是所有公司都披露折旧摊销。** 披露不了就报缺 ——
    用"行业平均折旧率"去填是最坏的做法：会得到一个
    看起来正常、实际无据的数，而且它还是 DCF 的核心输入。
    """
    oi = is_.get(Field.OPERATING_INCOME)
    da = is_.get(Field.DEPRECIATION_AMORTIZATION)
    if oi is None:
        return Derived("EBITDA", None, missing=[Field.OPERATING_INCOME])
    if da is None:
        return Derived(
            "EBITDA", None, missing=[Field.DEPRECIATION_AMORTIZATION],
            note="报表未披露折旧摊销；用行业平均去填会得到无据的数",
        )
    return Derived("EBITDA", oi + da,
                   parts=[(Field.OPERATING_INCOME.value, oi),
                          (Field.DEPRECIATION_AMORTIZATION.value, da)])


def derive_all(bal: dict[Field, float],
               is_: dict[Field, float] | None = None) -> list[Derived]:
    """一次推全部。"""
    out = [net_debt(bal), minority_interest(bal)]
    if is_ is not None:
        out.append(ebitda(is_))
    return out
