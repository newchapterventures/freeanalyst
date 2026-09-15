"""勾稽校验 —— 映射对不对的**客观判定标准**。

## 为什么这是这一层的验收标准

「理解三张表每一行」最贵的地方在于**会计科目不是确定清单** ——
公司会自创科目名，所以映射永远可能有漏。

但勾稽关系给了我们一个**免费的、机器可判定的**质量指标（spec §4.5）：

    资产 = 负债 + 所有者权益
    期末现金 = 期初 + 经营 + 投资 + 筹资（+ 汇率影响）
    净利润 → 经营现金流的间接法调节

**平了，说明映射对了。** 平不了，说明某一行归属错了 ——
而且差额能帮你定位是哪一行。

## 第三条为什么不逐行映射

间接法调节那段有十几行（折旧、摊销、股份支付、各项营运资本变动…），
逐行映射既费力又容易漏。

改成**把两个锚点之间的行全部加总**：

    净利润 + Σ(中间各行) = 经营现金流

这样即使某一行没被识别，校验依然有效 —— 因为它验的是**整段的完整性**，
不是逐行的正确性。

中间如果有小计行会被重复计算，所以要把「合计」「Total」这类
**汇总行**排除掉（否则一段加了两遍）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import Field


@dataclass
class Articulation:
    """一条勾稽关系的结果。"""

    name: str
    ok: bool | None            # None = 数据不足，判不了（不是失败）
    lhs: float | None = None
    rhs: float | None = None
    diff: float | None = None
    missing: list[Field] = field(default_factory=list)
    note: str = ""
    #: 这条校验适不适用。A 股现金流量表是直接法编的，
    #: 「净利润 → 经营现金流」那段根本不在这张表里 ——
    #: 报「不适用」和报「数据不足」对用户的意义完全不同：
    #: 前者不是问题，后者要去补数据。
    applicable: bool = True

    @property
    def status(self) -> str:
        if not self.applicable:
            return "不适用"
        if self.ok is None:
            return "数据不足"
        return "平" if self.ok else f"不平（差 {self.diff:+,.0f}）"

    def render(self, unit: str = "") -> str:
        lines = [f"{self.name}：{self.status}"]
        if self.lhs is not None and self.rhs is not None:
            lines.append(f"    {self.lhs:>16,.0f}  vs  {self.rhs:>16,.0f}{unit}")
        if self.missing:
            names = "、".join(f.value for f in self.missing)
            lines.append(f"    缺：{names}")
        if self.note:
            lines.append(f"    {self.note}")
        return "\n".join(lines)


def _get(d: dict[Field, float], f: Field) -> float | None:
    return d.get(f)


def _close(a: float, b: float, tol_ratio: float = 1e-6, min_tol: float = 1.0) -> bool:
    """相等判定。**给容差是为了容忍报表本身的四舍五入**，不是放松检查。

    单位是「万元」时 1.0 的容差对几十万级的数字来说可以忽略；
    但如果差的是几千几万，那一定是映射错了，不会因为容差放过去。
    """
    return abs(a - b) <= max(abs(a) * tol_ratio, min_tol)


def check_balance(bal: dict[Field, float]) -> Articulation:
    """资产 = 负债 + 所有者权益。

    ## 少数股东权益要不要另加，**两种口径都试**（实测踩到）

    「所有者权益合计」在不同准则下含义不同：

        CAS（A 股）    含少数股东权益 → 负债 + 权益 = 资产
        US GAAP        常为母公司的   → 负债 + 权益 + 少数股东权益 = 资产

    实测：贵州茅台先按「+少数股东权益」算，差 -9,321,442,877 ——
    **正好等于少数股东权益本身**，说明它已经被含在权益里了。
    而 Fitbit 那边少数股东权益为 0，两种算法都对。

    所以不能写死一种。**先试简单的，不成立再试加少数股东权益的** ——
    两条都试过还平不了，才报不平。
    """
    assets = _get(bal, Field.TOTAL_ASSETS)
    liab = _get(bal, Field.TOTAL_LIABILITIES)
    equity = _get(bal, Field.EQUITY)
    minority = _get(bal, Field.MINORITY_INTEREST) or 0.0

    missing = [f for f, v in ((Field.TOTAL_ASSETS, assets),
                              (Field.TOTAL_LIABILITIES, liab),
                              (Field.EQUITY, equity)) if v is None]
    if missing:
        return Articulation("资产 = 负债 + 所有者权益", None, missing=missing)

    assert assets is not None and liab is not None and equity is not None

    # 先试「权益已含少数股东权益」
    if _close(assets, liab + equity):
        return Articulation(
            "资产 = 负债 + 所有者权益", True,
            lhs=assets, rhs=liab + equity, diff=0.0,
            note="权益口径：已含少数股东权益（CAS 常见）",
        )

    rhs = liab + equity + minority
    return Articulation(
        "资产 = 负债 + 所有者权益", _close(assets, rhs),
        lhs=assets, rhs=rhs, diff=assets - rhs,
        note="权益口径：不含少数股东权益（需另加）" if minority else "",
    )


def check_cash_rollforward(
    cf: dict[Field, float],
    cash_begin: float | None,
    cash_end: float | None,
) -> Articulation:
    """期末现金 = 期初 + 经营 + 投资 + 筹资（+ 汇率影响）。

    这是**三张表的连接点**：期初期末来自资产负债表，中间三块来自现金流量表。
    """
    cfo = _get(cf, Field.CFO)
    cfi = _get(cf, Field.CFI)
    cff = _get(cf, Field.CFF)
    fx = _get(cf, Field.FX_EFFECT) or 0.0

    missing = []
    if cash_begin is None:
        missing.append(Field.CASH_BEGIN)
    if cash_end is None:
        missing.append(Field.CASH_END)
    for f, v in ((Field.CFO, cfo), (Field.CFI, cfi), (Field.CFF, cff)):
        if v is None:
            missing.append(f)
    if missing:
        return Articulation("期末现金 = 期初 + 经营 + 投资 + 筹资", None, missing=missing)

    assert cash_begin is not None and cash_end is not None
    assert cfo is not None and cfi is not None and cff is not None
    rhs = cash_begin + cfo + cfi + cff + fx
    note = "" if fx == 0 else f"（含汇率影响 {fx:+,.0f}）"
    return Articulation(
        "期末现金 = 期初 + 经营 + 投资 + 筹资", _close(cash_end, rhs),
        lhs=cash_end, rhs=rhs, diff=cash_end - rhs, note=note,
    )


#: 汇总行 —— 间接法调节段里如果出现这些小计，加总会重复计算
_TOTALISH = ("合计", "小计", "总计", "total", "subtotal", "net cash",
             "adjustments to reconcile", "调整项目")


#: 直接法的标志行 —— 有这些行说明是直接法编的现金流量表
_DIRECT_METHOD_MARKERS = (
    "销售商品、提供劳务收到的现金",
    "经营活动现金流入小计",
    "购买商品、接受劳务支付的现金",
    "收到的税费返还",
    "cash received from customers",
    "cash paid to suppliers",
)


def is_direct_method(rows: list[tuple[str, float | None]]) -> bool:
    """判断现金流量表是直接法还是间接法。

    ## 这是 A 股和美股的一个根本差异（实测踩到）

    美股现金流量表用**间接法**：从净利润出发，加回折旧摊销、
    调整营运资本变动，最后得到经营现金流 —— 于是「净利润 → 经营现金流」
    这条勾稽**可以逐行验**。

    A 股用**直接法**：直接列「销售商品收到的现金」「购买商品支付的现金」，
    中间没有那段调节（间接法调节在**附注**里）。

    对 A 股报表硬跑间接法检查，会报「定位不到锚点」——
    看着像映射漏了，其实是**这个方法不适用**。
    两者必须区分：一个是待修的问题，一个不是问题。
    """
    labels = [(l or "") for l, _ in rows]
    return any(m in l for l in labels for m in _DIRECT_METHOD_MARKERS)


def _is_totalish(label: str) -> bool:
    low = label.lower()
    return any(k in low for k in _TOTALISH)


def check_indirect_method(
    rows: list[tuple[str, float | None]],
    net_income: float | None,
    cfo: float | None,
) -> Articulation:
    """净利润 → 经营现金流的间接法调节。

    `rows` 是现金流量表里**净利润行与经营现金流行之间**的所有行，
    按出现顺序 `[(行名, 金额), …]`（金额为 None 的跳过）。

    ## 为什么不逐行映射

    中间有十几行（折旧、摊销、股份支付、各项营运资本变动…），
    逐行映射既费力又容易漏。改成**整段加总**：
    只要段内所有行都被正确抽取，和就一定对得上 ——
    验的是**整段的完整性**，不是逐行的正确性。

    汇总行要排除，否则重复计算。
    """
    if net_income is None or cfo is None:
        missing = []
        if net_income is None:
            missing.append(Field.NET_INCOME)
        if cfo is None:
            missing.append(Field.CFO)
        return Articulation("净利润 → 经营现金流（间接法）", None, missing=missing)

    total = net_income
    counted = 0
    skipped_totalish: list[str] = []
    for label, value in rows:
        if value is None:
            continue
        if _is_totalish(label):
            skipped_totalish.append(label)
            continue
        total += value
        counted += 1

    note = f"段内加总 {counted} 行"
    if skipped_totalish:
        note += f"；跳过汇总行 {len(skipped_totalish)} 行（重复计算）"
    return Articulation(
        "净利润 → 经营现金流（间接法）", _close(cfo, total),
        lhs=cfo, rhs=total, diff=cfo - total, note=note,
    )
