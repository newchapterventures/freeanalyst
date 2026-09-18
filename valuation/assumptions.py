"""DCF 假设清单 —— **给用户逐条确认用**，不是给程序自动填的。

## 用户的原话（2026-09-17）

> DCF 假设有几个主要的，也就是你提到的这几个，还会有一些其他的，
> 这些**需要跟用户讨论**。**尤其收入增速，这个需要参考外部数据，
> 比如可比公司的数据。**

## 这份清单存在的理由

估值最容易错的地方不在公式，在**假设**。同一个标的、同一套现金流，
折现率差 2 个点、永续增长差 1 个点，结论能差一倍以上 ——
而**这些数字看起来都「合理」**。

所以每条假设都给五样东西：

    drives      它影响什么（为什么不能随便拍）
    history     从报表算出来的历史值（已经有了）
    external    需要外部数据的地方（比如收入增速 ← 可比公司）
    check       怎么校验（跟什么比、不能超过什么）
    trap        最容易拍错的地方

**能自动算的自动算，算不出的明说算不出** —— 不用默认值填。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Group(str, Enum):
    REVENUE = "收入侧"
    MARGIN = "利润与资本侧"
    DISCOUNT = "折现侧"
    BRIDGE = "口径与桥接"


class Need(str, Enum):
    """这个假设的数据从哪来 —— **决定它是自动填还是要问用户**。"""

    COMPUTED = "报表算得出"
    EXTERNAL = "需要外部数据"
    USER = "要用户定"
    BOTH = "报表起步，用户调整"


@dataclass
class Assumption:
    key: str
    label: str
    group: Group
    need: Need
    drives: str
    check: str
    trap: str
    #: 从报表算出来的历史值（没算出来就是 None，**不用默认值填**）
    value: float | None = None
    #: 这个值怎么来的（一句话，含页码或科目名）
    basis: str = ""
    #: 置信度：高 / 中 / 低 / —
    confidence: str = "—"
    #: **静态**：这条为什么需要外部数据（定义的一部分，永远在）
    external_need: str = ""
    #: **动态**：现在还缺什么（数据接上了就清空）
    external_gap: str = ""
    #: 用户填的（有值就表示用户已确认）
    user_value: float | None = None

    @property
    def resolved(self) -> bool:
        return self.user_value is not None or self.value is not None

    def line(self, fmt: str = ",.0f") -> str:
        v = self.user_value if self.user_value is not None else self.value
        if v is None:
            return "缺"
        return format(v, fmt)


#: DCF 假设表 —— 顺序就是评估顺序（先收入，再利润，再折现，最后桥接）。
DCF_ASSUMPTIONS: tuple[Assumption, ...] = (
    # ─────────── 收入侧 ───────────
    Assumption(
        key="revenue_growth",
        label="收入增速（显式预测期，一般 5 年，逐年给）",
        group=Group.REVENUE,
        need=Need.EXTERNAL,
        drives="DCF 里权重最大的一项 —— 它决定终值之前全部现金流的规模，"
               "而且通过终值再放大一次",
        check="① 跟历史增速比（差太多要有理由）"
              "② 跟**可比公司**同期增速比（这是你要求的那条）"
              "③ 长期不能超过名义 GDP 增速 + 行业集中度提升的空间",
        trap="**拍一条前高后低的曲线最容易骗自己。** 前三年 30%、第四年突然掉到 5%，"
             "看起来「保守」，实际是把增长全压在不需要折现太狠的近期，"
             "终值又被后段拖低 —— 中间的不连续在敏感性分析里看不见。",
        external_need="**要比可比公司的收入增速** —— 需要先定可比公司",
    ),
    Assumption(
        key="terminal_growth",
        label="永续增长率（g）",
        group=Group.REVENUE,
        need=Need.USER,
        drives="终值通常占 DCF 总值的 60–80%，**g 差 1 个点，估值能差 20% 以上**",
        check="**绝不能超过长期名义 GDP 增速**（中国参考 3–4%，美国参考 2–3%）。"
              "超过就等于假设这家公司永远比整个经济长得快 —— 数学上不成立。",
        trap="拿历史高增速当 g。**永续增长率不是「未来还能长多快」，"
             "是「无限期之后还能不能跑赢通胀」。** 成熟市场里能长期跑赢通胀的"
             "公司极其罕见。",
    ),
    Assumption(
        key="revenue_driver",
        label="收入驱动方式（总量 / 分业务分别预测）",
        group=Group.REVENUE,
        need=Need.USER,
        drives="分业务预测能看出结构变化；总量预测快但会把结构变化平均掉",
        check="**有多个毛利率差异大的业务板块时必须分拆** —— 否则利润率假设会失真。"
              "单业务或各板块毛利率接近时可以走总量。",
        trap="各业务增速不同、毛利率也不同（比如广告代理毛利 5%、"
             "营销科技毛利 40%），走总量预测会**同时错掉收入和利润率两个假设**，"
             "而且看不出来是哪个错的。",
    ),

    # ─────────── 利润与资本侧 ───────────
    Assumption(
        key="ebitda_margin",
        label="EBITDA 利润率",
        group=Group.MARGIN,
        need=Need.BOTH,
        drives="经营效率假设；也决定 EBITDA 基数，进而影响倍数法的可比性",
        check="跟历史利润率比、跟可比公司比；**规模上去以后利润率该不该改善、"
              "改善多少**（规模效应是有限度的）",
        trap="**把「最好的那一年」当常态。** 某年因为费用递延或收入确认时点"
             "导致利润率异常高，拿它当预测基准会系统性高估。要用 3–5 年平均。",
    ),
    Assumption(
        key="da_pct",
        label="折旧摊销（D&A）占收入比",
        group=Group.MARGIN,
        need=Need.COMPUTED,
        drives="EBITDA → EBIT 的桥；也是重资产程度和未来 CapEx 的交叉验证",
        check="跟 CapEx/收入比对比 —— **长期看 D&A 应该大致等于 CapEx**"
              "（重资产公司尤其）。差太远说明有一头假设错了。",
        trap="**D&A 在年报里出现多次，且含义不同。** 管理费用明细里的"
             "「固定资产折旧费用」只是计入管理费用的那一部分，"
             "拿它当全公司 D&A 会严重低估。要用现金流量表附注"
             "「将净利润调节为经营活动现金流量」段里的数。",
    ),
    Assumption(
        key="capex_pct",
        label="资本支出（CapEx）占收入比",
        group=Group.MARGIN,
        need=Need.BOTH,
        drives="自由现金流的直接扣减项；重资产公司的关键变量",
        check="跟历史 CapEx 比、跟 D&A 比、跟**产能扩张计划**比。"
              "增长期的 CapEx 通常高于 D&A，稳定期趋近。",
        trap="**用「购建固定资产、无形资产和其他长期资产支付的现金」"
             "就等于 CapEx 是不够的** —— 兼并收购不在这里面，"
             "但维持性 CapEx 和扩张性 CapEx 也分不开。要问清增长假设"
             "对应多少产能投入。",
    ),
    Assumption(
        key="nwc_pct",
        label="营运资本占收入比（ΔNWC）",
        group=Group.MARGIN,
        need=Need.BOTH,
        drives="增长要从现金流里抽走多少 —— 增长越快，抽得越多",
        check="用「（应收 + 存货 − 应付）/ 收入」算历史值。"
              "**增长假设越高，营运资本抽血越多**，两者要一起看。",
        trap="**把营运资本变化当成零。** 高增长公司里 ΔNWC 往往吃掉一半以上的"
             "增量经营现金流 —— 漏掉它，DCF 结果会高出很多，"
             "**而且在高增长假设下错得最厉害**（正好是最需要准确的时候）。",
    ),
    Assumption(
        key="tax_rate",
        label="实际税率",
        group=Group.MARGIN,
        need=Need.COMPUTED,
        drives="EBIT → 税后现金流；也影响 WACC 里的税盾（Kd × (1−t)）",
        check="用「所得税费用 / 利润总额」算，**不要用法定税率 25%**。"
              "有税收优惠、亏损弥补、境外税率差异的要用实际值。",
        trap="亏损年份或有大额递延所得税调整的年份，实际税率会异常，"
             "**不能简单拿去预测** —— 要用正常化后的税率。",
    ),

    # ─────────── 折现侧 ───────────
    Assumption(
        key="wacc",
        label="加权平均资本成本（WACC）",
        group=Group.DISCOUNT,
        need=Need.USER,
        drives="**折现率是 DCF 最敏感的输入** —— 通常 ±1 个点能带来 ±15–25% 的估值变化",
        check="拆开看每一项：Rf（无风险利率）、ERP（股权风险溢价）、"
              "β、Kd（债务成本）、D/E（资本结构）、t（税率）。"
              "**每一项单独都能跟公开数据对**，不要只报一个 WACC 数。",
        trap="① 用「目标资本结构」还是「当前资本结构」，两套结果不一样，要说明用哪套。"
             "② β 用可比公司的（去杠杆再加杠杆），不是标的自己的历史 β ——"
             "非上市公司根本没有。"
             "③ **Rf 要和现金流的币种一致** —— 用人民币现金流配美国国债利率是错的。",
    ),
    Assumption(
        key="exit_method",
        label="终值算法（永续增长 / 退出倍数）",
        group=Group.DISCOUNT,
        need=Need.USER,
        drives="终值占 DCF 总值的 60–80%，换算法能差一倍",
        check="**两种都算，摆在足球场上做区间** —— 只报一种等于把方法差异藏起来了",
        trap="**退出倍数法容易循环论证**：拿当前的 EV/EBITDA 当退出倍数，"
             "等于假设倍数是永恒不变的 —— 那估值高低就全看当前倍数了。"
             "退出倍数通常该**低于**当前倍数（公司成熟了、增长慢了）。",
    ),

    # ─────────── 口径与桥接（用户说的「还会有一些其他的」）───────────
    Assumption(
        key="net_debt_scope",
        label="净债务口径（含 / 不含租赁负债）",
        group=Group.BRIDGE,
        need=Need.USER,
        drives="EV → 股权价值的桥，**直接加减在最终结论上**",
        check="国际准则下租赁负债要计入债务（IFRS 16 之后租金费用资本化了）；"
              "中国准则下视情况。**同一份分析里口径必须一致**。",
        trap="**EV 和 EBITDA 的口径要配套。** 租赁负债计入债务了，"
             "那 EBITDA 里的租金费用也应该加回（因为已经资本化成折旧和利息了）。"
             "一头做一头不做，EV/EBITDA 会失真。",
    ),
    Assumption(
        key="minority_interest",
        label="少数股东权益处理",
        group=Group.BRIDGE,
        need=Need.COMPUTED,
        drives="EV 里要减掉少数股东权益，才是归属母公司股东的价值",
        check="用资产负债表上的「少数股东权益」科目，"
              "**不是利润表上的「少数股东损益」** —— 两个数是不同的东西",
        trap="**这个口径差能到几十亿。** 有一家材料上差 93 亿 ——"
             "要确认拿到的是资产负债表那个（存量），不是利润表那个（流量）。",
    ),
    Assumption(
        key="sbc",
        label="股份支付（SBC）算不算成本",
        group=Group.BRIDGE,
        need=Need.USER,
        drives="科技/服务类公司的调整项，**能不能改进 EBITDA 差别很大**",
        check="现金流量表补充资料里有「股份支付」这一行（`ID_STOCK_COMP`）",
        trap="**「加回股份支付」是常见的美化手段。** 股份支付是真实成本"
             "（稀释了股东），国际准则下已经计入费用。"
             "加回后 EBITDA 好看，但那不是「经营口径」而是「非现金口径」。"
             "**要用就统一用**，别在标的这边加回、在可比公司那边不加。",
    ),
    Assumption(
        key="nonrecurring",
        label="非经常性损益要不要剔除",
        group=Group.BRIDGE,
        need=Need.USER,
        drives="决定用「报表口径」还是「可持续口径」的利润",
        check="看营业外收支、投资收益、资产处置损益的规模和持续性",
        trap="**两头都剔除才能比。** 只把标的的政府补助剔了、"
             "可比公司的不剔，可比性就没了。规则要对称。",
    ),
    Assumption(
        key="midyear",
        label="折现时点（年末惯例 / 期中惯例）",
        group=Group.BRIDGE,
        need=Need.USER,
        drives="期中惯例把第一年现金流多折半年，**高估值能差 3–5%**",
        check="现金是全年陆续流入的，用期中惯例更贴近现实；"
              "但**要跟可比公司的算法一致**",
        trap="这是个「小」假设，但结果差异是**系统性单向的** ——"
             "期中惯例永远算出更高的估值。换个惯例看起来像「技术调整」，"
             "实际是悄悄抬价。",
    ),
    Assumption(
        key="fx",
        label="汇率假设（有海外业务时）",
        group=Group.BRIDGE,
        need=Need.USER,
        drives="海外收入折算、外币债务重估",
        check="**用远期汇率还是即期汇率要说明**；预测期汇率变化要有依据",
        trap="用当前汇率一路外推，等于假设汇率永远不变。"
             "汇率波动大的行业（出海业务）要单独做汇率敏感性。",
    ),
)


def build_checklist(
    statements=None,
    *,
    comps_ready: bool = False,
) -> list[Assumption]:
    """从报表填能自动算的，其余留空并说明缺什么。

    **填不出来的不填** —— 用默认值填出来的清单看起来完整，
    但那会让用户以为某项已经定了，实际是个假数。
    """
    out = [Assumption(**{**a.__dict__}) for a in DCF_ASSUMPTIONS]

    if statements is not None:
        _fill_from_statements(out, statements)

    # 动态缺口：没接可比公司就标出来，接了就清掉。
    # **跟 `external_need` 分开** —— 后者是「这条为什么需要外部数据」，
    # 是定义的一部分，永远都在；混在一起会让「接上了」没法表达。
    for a in out:
        if a.need is Need.EXTERNAL and not a.value and not comps_ready:
            a.external_gap = a.external_need or "需要可比公司数据，还没接上"
    return out


def _fill_from_statements(out: list[Assumption], S) -> None:
    """把能从三张表算出来的填进去。算不出来就留着空。"""
    from financials import derive
    from financials.canonical import Field

    def get(st, f):
        return (st.fields.get(f) if st else None)

    bal, inc, cf = S.balance, S.income, S.cash_flow
    rev = get(inc, Field.REVENUE)
    by = {a.key: a for a in out}

    # ── 收入增速：报表里只有本期和上期两列，只能给一期历史增速
    a = by["revenue_growth"]
    prev = _prior(inc, Field.REVENUE)
    if rev and prev:
        g = rev / prev - 1.0
        a.basis = (f"本期营业收入 {format(rev, ',.0f')}，"
                   f"上期 {format(prev, ',.0f')} → 同比 {g * 100:+.1f}%"
                   f"（**只有一期历史，不能外推成预测期曲线**）")
        a.confidence = "中"
    else:
        a.basis = "报表里取不到本期和上期两期收入，算不出历史增速"
        a.confidence = "—"

    # ── EBITDA 利润率
    a = by["ebitda_margin"]
    merged = dict(inc.fields) if inc else {}
    if cf:
        merged.update(cf.fields)
    if S.da is not None and getattr(S.da, "total", None) is not None:
        merged.setdefault(Field.DEPRECIATION_AMORTIZATION, S.da.total)
    e = derive.ebitda(merged)
    if e.value and rev:
        a.value = e.value / rev
        a.basis = (f"EBITDA {format(e.value, ',.0f')} / 营业收入 "
                   f"{format(rev, ',.0f')} = {a.value * 100:.1f}%")
        a.confidence = "中" if "折旧" not in (e.note or "") else "低"
    else:
        a.basis = "算不出 EBITDA：" + (e.note or "缺营业利润或折旧摊销")
        a.confidence = "—"

    # ── D&A 占收入
    a = by["da_pct"]
    da = getattr(S.da, "total", None) if S.da is not None else None
    if da and rev:
        a.value = da / rev
        a.basis = (f"附注调整段 D&A {format(da, ',.0f')} / 收入 "
                   f"{format(rev, ',.0f')} = {a.value * 100:.2f}%")
        a.confidence = "高"
    else:
        a.basis = ("附注里没找到「将净利润调节为经营活动现金流量」段，"
                   "取不到 D&A")
        a.confidence = "—"

    # ── CapEx 占收入
    a = by["capex_pct"]
    capex = get(cf, Field.CAPEX)
    if capex and rev:
        a.value = abs(capex) / rev
        a.basis = (f"现金流量表 CapEx {format(abs(capex), ',.0f')} / 收入 "
                   f"{format(rev, ',.0f')} = {a.value * 100:.2f}%")
        a.confidence = "中"
    else:
        a.basis = "现金流量表里取不到「购建固定资产…支付的现金」"
        a.confidence = "—"

    # ── 营运资本占收入
    a = by["nwc_pct"]
    if bal and rev:
        ar = get(bal, Field.ACCOUNTS_RECEIVABLE) or 0.0
        inv = get(bal, Field.INVENTORY) or 0.0
        ap = get(bal, Field.ACCOUNTS_PAYABLE) or 0.0
        if ar or inv or ap:
            a.value = (ar + inv - ap) / rev
            a.basis = (f"(应收 {format(ar, ',.0f')} + 存货 {format(inv, ',.0f')}"
                       f" − 应付 {format(ap, ',.0f')}) / 收入 "
                       f"{format(rev, ',.0f')} = {a.value * 100:.1f}%"
                       f"｜**这是存量占比，ΔNWC 要看两期之差**")
            a.confidence = "中"
        else:
            a.basis = "资产负债表里取不到应收 / 存货 / 应付"
    else:
        a.basis = "缺资产负债表或收入，算不出"

    # ── 实际税率
    a = by["tax_rate"]
    tax, pretax = get(inc, Field.INCOME_TAX), get(inc, Field.PRETAX_INCOME)
    if tax is not None and pretax:
        a.value = tax / pretax
        a.basis = (f"所得税费用 {format(tax, ',.0f')} / 利润总额 "
                   f"{format(pretax, ',.0f')} = {a.value * 100:.1f}%"
                   f"｜**单年值，有递延所得税调整时会失真**")
        a.confidence = "中"
    else:
        a.basis = "取不到所得税费用或利润总额"

    # ── 少数股东权益
    a = by["minority_interest"]
    mi = get(bal, Field.MINORITY_INTEREST)
    if mi is not None:
        a.value = mi
        a.basis = f"资产负债表「少数股东权益」{format(mi, ',.0f')}"
        a.confidence = "高"
    else:
        a.basis = "资产负债表里没有少数股东权益科目（无子公司少数股东，或未识别）"


def _prior(inc, f) -> float | None:
    """取上期值。`StatementSet.columns` 有「期末/期初」两列时才有。"""
    if inc is None:
        return None
    priors = getattr(inc, "prior", None)
    if isinstance(priors, dict):
        return priors.get(f)
    cols = getattr(inc, "columns", None)
    if cols and len(cols) > 1:
        return None
    return None


def render_checklist(items: list[Assumption]) -> str:
    """给用户看的假设清单。**这是互动环节的正文。**"""
    out = []
    groups = [Group.REVENUE, Group.MARGIN, Group.DISCOUNT, Group.BRIDGE]
    done = sum(1 for a in items if a.resolved)
    out.append(f"══════ DCF 假设清单　{len(items)} 条，"
               f"其中 {done} 条报表能自动填 ══════")
    out.append("")
    for gi, g in enumerate(groups, 1):
        rows = [a for a in items if a.group is g]
        if not rows:
            continue
        out.append(f"【{'一二三四'[gi - 1]}、{g.value}】")
        out.append("")
        for i, a in enumerate(rows, 1):
            tag = {"报表算得出": "可自动填", "需要外部数据": "**缺外部数据**",
                   "要用户定": "**要你定**", "报表起步，用户调整": "报表起步"}[a.need.value]
            out.append(f"{i}. {a.label}　〔{tag}〕")
            out.append(f"   影响：{a.drives}")
            if a.value is not None:
                fmt = ".1%" if a.key in ("ebitda_margin", "da_pct", "capex_pct",
                                         "nwc_pct", "tax_rate") else ",.0f"
                out.append(f"   报表值：{a.line(fmt)}　（置信度 {a.confidence}）")
            out.append(f"   依据：{a.basis or '—'}")
            if a.external_need:
                out.append(f"   需外部数据：{a.external_need}")
            if a.external_gap and a.external_gap != a.external_need:
                out.append(f"   ⬜ 当前缺口：{a.external_gap}")
            out.append(f"   怎么校验：{a.check}")
            out.append(f"   ⚠ 最容易拍错：{a.trap}")
            out.append("")
    n_user = sum(1 for a in items if a.need in (Need.USER, Need.BOTH)
                 and a.user_value is None)
    out.append(f"── 需要你确认或给值的：{n_user} 条 "
               f"／ 需要外部数据的："
               f"{sum(1 for a in items if a.external_gap)} 条")
    return "\n".join(out)
