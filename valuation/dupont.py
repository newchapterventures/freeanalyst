"""杜邦分析：把 ROE 拆成几个能单独看的因素。

## 为什么它便宜
估值要十几个数，杜邦只要其中**四个**：净利 / 收入 / 总资产 / 权益。
实测五份材料（含港股 IFRS、含扫描件）这四个数**全都有** —— 所以它不依赖任何新数据源。

## 两个口径，别混着说
    三因子：ROE = 净利率 × 总资产周转率 × 权益乘数
    五因子：ROE = 税负 × 息税前利润率 × 利息负担 × 周转率 × 权益乘数
默认两个都给：五因子信息多，三因子好讲。

## 金融企业**不给数**（这条最重要）
银行/保险的权益乘数动辄 15–20 倍。把它们的 ROE 拆成"净利率 × 周转率 × 杠杆"，
结论永远只有一句"它杠杆很高" —— 那不是分析，是把定义换个说法念一遍。
所以金融企业**明确返回不适用**，并把命中的信号写清楚；
宁可说"这个指标对这类公司没有意义"，也不给一个看着像结论的数。

信号只从**资产负债表**判（实测：利润表判不出行业 —— 工行和中信的通用利润表
都有数据），而且要看**金额占比**，不是看有没有这个词。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from financials.canonical import Field


@dataclass
class DuPont:
    """一份杜邦分解结果。算不出来的项一律留 None + 说明，**不填 0**。"""

    applicable: bool = True
    why_not: str = ""
    signs: list[str] = field(default_factory=list)     # 命中"这是金融企业"的信号
    years: str = ""
    unit: str = ""

    #: 输入（原样留着，方便复核）
    inputs: dict[str, float] = field(default_factory=dict)
    #: 五因子
    tax_burden: float | None = None        # 净利 / 税前利润
    ebit_margin: float | None = None       # 税前利润 / 收入
    interest_burden: float | None = None   # 税前利润 / 营业利润
    asset_turnover: float | None = None    # 收入 / 总资产
    equity_multiplier: float | None = None # 总资产 / 权益
    #: 三因子
    net_margin: float | None = None        # 净利 / 收入
    roe: float | None = None
    roa: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        if not self.applicable:
            return [f"杜邦分析：**不适用** —— {self.why_not}"]
        out = [f"杜邦分析（{self.years}｜单位 {self.unit}）"]
        out.append(f"  ROE = 净利率 {self._p(self.net_margin)} × 周转率 "
                   f"{self._n(self.asset_turnover)} × 权益乘数 {self._n(self.equity_multiplier)}"
                   f"  = {self._p(self.roe)}")
        out.append("  五因子拆解：")
        for label, v, kind in (("税负（净利/税前）", self.tax_burden, "p"),
                               ("息税前利润率", self.ebit_margin, "p"),
                               ("利息负担（税前/营业利润）", self.interest_burden, "n"),
                               ("总资产周转率", self.asset_turnover, "n"),
                               ("权益乘数", self.equity_multiplier, "n")):
            out.append(f"    {label:24} {self._p(v) if kind == 'p' else self._n(v)}")
        out.append(f"  （ROA = {self._p(self.roa)}）")
        for n in self.notes:
            out.append(f"  · {n}")
        return out

    @staticmethod
    def _p(v: float | None) -> str:
        return "—" if v is None else f"{v * 100:.2f}%"

    @staticmethod
    def _n(v: float | None) -> str:
        return "—" if v is None else f"{v:.3f}"


#: 金融企业**不靠猜**。
#:
#: ## ★ 我在这里重犯了一次项目已经记录过的错（值得留着）
#:
#: 第一版我用「`其他非流动负债` 占总负债 ≥30%」当保险信号 ——
#: 结果**国城矿业（一家矿业公司）被判成金融企业** ✗，而它的表里
#: 「其他非流动负债」确实占 51%（那是它的专项应付款/预计负债之类）。
#:
#: 而项目里 `financials/meta.py` 的注释早就写过这件事：
#:
#:     试过两版判据，都不成立：
#:       1. 喂整份正文 → 某 377 页矿业公司审计报告…→ 矿业公司被判成金融 ✗
#:       2. 喂三张表的行标签 → 仍然误判（集团旗下真有金融/保险业务）
#:     **误判的代价大于不判**：它会把一份完全正常的材料标成"口径不适用"，
#:     把参考值全关掉 —— 比原来的"显示数值"更坏。
#:
#: 所以这里**不自动判定**。要做对需要"金融科目占主体"这类占比判据，
#: 而那必须先拿多份真材料量阈值 —— 在量出来之前，保持不做。
#:
#: 替代做法（对用户真的有用，而且不会误判）：照常给数，
#: **但在权益乘数很高时明确提示**"这家杠杆很高，三因子意义有限"。
#: 金融企业应由**用户声明**（用途/行业），而不是工具猜。

#: 权益乘数超过这个数，就在结果里提示"杠杆主导"。
#: 取值依据：实测 某非上市企业 2.28× / 一般工商企业 2–4×；
#: 到 8× 以上，ROE 基本由杠杆而非经营决定。
LEVERAGE_WARN = 8.0


def financial_signs(S) -> list[str]:
    """**保留函数名但不再自动判**（见上面那段：误判比不判更坏）。

    返回空表 —— 也就是说"从材料本身看不出是不是金融企业"，
    这件事交给用户声明。
    """
    return []


def _get(S, kind: str, f: Field) -> float | None:
    """读一个科目的值。

    ## ★ 实测踩到（值得留在这里）
    `StatementSet.fields` 是 **`Field -> 数字`** 的直接映射，**不是包装对象**。
    我第一版写成 `getattr(v, "value", None)` —— 于是**每一科都被读成 None**，
    五份材料齐刷刷报"缺营业收入/净利润/总资产/权益"。

    更坏的是：**我的单元测试全过了** —— 因为我在测试里自己造了一个带 `.value`
    的包装对象。测试量的是我**假想的结构**，不是真实的结构。
    所以这里对两种形状都认，而测试改成**用真实形状**（见 tests/test_dupont.py）。
    """
    st = getattr(S, kind, None)
    if st is None:
        return None
    v = (getattr(st, "fields", {}) or {}).get(f)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    inner = getattr(v, "value", None)        # 万一是包装对象（兼容，不是主路）
    if isinstance(inner, (int, float)) and not isinstance(inner, bool):
        return float(inner)
    return None


def analyse(S) -> DuPont:
    """按一份 `Statements` 做杜邦分解。缺项一律留 None 并写进 notes，**不填 0**。"""
    d = DuPont(unit=getattr(S, "unit", "") or "", years=getattr(S, "period", "") or "")

    rev = _get(S, "income", Field.REVENUE)
    ni = _get(S, "income", Field.NET_INCOME)
    pretax = _get(S, "income", Field.PRETAX_INCOME)
    op = _get(S, "income", Field.OPERATING_INCOME)
    assets = _get(S, "balance", Field.TOTAL_ASSETS)
    equity = _get(S, "balance", Field.EQUITY) or _get(S, "balance", Field.EQUITY_PARENT)

    for name, v in (("营业收入", rev), ("净利润", ni), ("利润总额", pretax),
                    ("总资产", assets), ("所有者权益", equity)):
        if v is not None:
            d.inputs[name] = v

    # 金融企业**不自动判**（见 `financial_signs` 上面那段：项目里已经踩过，
    # 误判的代价大于不判）。所以这里不再有"看起来像金融企业 → 不给数"的分支；
    # 改成在**权益乘数很高**时明确提示，让人自己判断该不该用三因子。
    missing = [n for n, v in (("营业收入", rev), ("净利润", ni), ("总资产", assets),
                              ("所有者权益", equity)) if v is None]
    if missing:
        d.applicable = False
        d.why_not = f"这几个数没取到：{'、'.join(missing)}（缺就不算，不拿 0 填）"
        return d
    if not rev or not assets or not equity or rev <= 0 or assets <= 0 or equity <= 0:
        d.applicable = False
        d.why_not = "营业收入/总资产/所有者权益里有非正数，比率没有意义"
        return d

    # 走到这里四个数都非 None（上面查过 missing）—— 断言只是把这条不变式
    # 告诉类型检查，不是兜底。
    assert (rev is not None and ni is not None
            and assets is not None and equity is not None)

    d.net_margin = ni / rev
    d.asset_turnover = rev / assets
    d.equity_multiplier = assets / equity
    d.roe = d.net_margin * d.asset_turnover * d.equity_multiplier
    d.roa = ni / assets

    if pretax is not None and ni is not None:
        d.tax_burden = ni / pretax if pretax else None
    if pretax is not None and rev:
        d.ebit_margin = pretax / rev
    if op is not None and pretax is not None:
        d.interest_burden = pretax / op if op else None

    if op is None or pretax is None:
        d.notes.append("营业利润或利润总额缺一项 → 五因子里那两项留空（三因子不受影响）")
    if d.equity_multiplier > LEVERAGE_WARN:
        d.notes.append(
            f"⚠️ 权益乘数 {d.equity_multiplier:.1f}× —— 到这个水平，ROE 主要由"
            f"**杠杆**决定，不是经营。三因子在这类公司上意义有限，"
            f"建议同时看 ROA 和利润来源。"
            f"（银行/保险尤其如此；工具**不替用户判断**公司类型。）")
    return d
