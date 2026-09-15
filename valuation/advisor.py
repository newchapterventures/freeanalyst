"""假设参谋 —— 这个产品真正的位置。

## 为什么这块是差异化的

竞品都在做「帮你算得更快」。**没有一家在解决「这个 WACC 到底该填多少」。**

- 最低层次：接受用户给的假设并计算 → **这是计算器，市面有几百个**
- 应有层次：给出**同类公司的分布**，标出你的假设落在第几分位
- 应有层次：列出**支撑这个假设需要什么证据**，去哪找，属于什么证据级别

这个模块做后两层。

## 数据从哪来

**同行业分布是真算出来的**，不是模型编的：

    从 SEC EDGAR 取一组可比公司 → 各自算收入 CAGR / EBITDA 率 → 分位数

这一层是本项目里唯一「数据优势」的地方 —— 免费、官方、结构化。
但**它只覆盖上市公司**，非上市标的自己的历史只能靠材料，算不出来就说算不出来。

## 三条纪律

1. **不用 LLM 生成对照数字。** 分布是算的。编一个"行业平均 12%"和
   编一个"公司未来增长 12%"一样危险。
2. **拿不到分布就说拿不到。** 硬凑一个参照系比没有参照系更糟 ——
   它会让用户以为自己有了依据。
3. **证据清单是知识，不是猜测。** 它来自尽调的通行做法（A/B/C/D 分级），
   写在代码里可以被人审阅和修改，不随模型随机性漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from datasources import sec_edgar as se

from .core import Assumption, Confidence, Trace


# ---------------------------------------------------------------------------
# 分布
# ---------------------------------------------------------------------------

@dataclass
class PeerStat:
    """一组可比公司在某个指标上的分布。"""

    metric: str
    values: list[float]
    labels: list[str]
    source: str
    unit: str = ""
    gaps: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.values)

    def quantile(self, q: float) -> float | None:
        vals = sorted(self.values)
        if not vals:
            return None
        if len(vals) == 1:
            return vals[0]
        pos = q * (len(vals) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(vals) - 1)
        return vals[lo] * (1 - (pos - lo)) + vals[hi] * (pos - lo)

    def median(self) -> float | None:
        return self.quantile(0.5)

    def percentile_of(self, v: float) -> float:
        """v 在这组分布里落在第几分位（0–1）。

        用「小于等于它的比例」，不是插值 —— 用户问的是
        「我的假设比多少家同行高」，那是个计数问题。
        """
        if not self.values:
            return float("nan")
        return sum(1 for x in self.values if x <= v) / len(self.values)

    def render(self, fmt: str = ".2%") -> str:
        if not self.values:
            return f"{self.metric}：无可用样本"
        f = lambda q: format(self.quantile(q), fmt)  # noqa: E731
        return (f"{self.metric}  （样本 {self.n} 家）\n"
                f"    P25 {f(0.25)}   中位 {f(0.5)}   P75 {f(0.75)}\n"
                f"    区间 {format(min(self.values), fmt)} – {format(max(self.values), fmt)}")


# ---------------------------------------------------------------------------
# 从 EDGAR 算指标
# ---------------------------------------------------------------------------

def revenue_cagr(facts: dict, years: int = 3, as_of: str | None = None) -> tuple[float | None, str]:
    """最近 N 年的营业收入复合增长率。

    返回 (CAGR, 说明)。**用实际天数折算，不假设「N 年正好 N 年」** ——
    财年长度会变（52/53 周制、财年变更）。

    `as_of` 给了就只用那天**已经披露**的财报。

    ## 为什么这个参数不是可选的（在对照场景里）

    假设参谋的用途是「拿标的的假设去比同行的历史」。如果比的是
    **当时还不存在**的数据，得出的分位数会让用户以为自己的假设很保守 ——
    而实际上他可能比那个时点的任何人都激进。

    **同一个项目里不能有两套口径**：comps 模块做了这个检查，
    这里也必须做。
    """
    for tag in se._REVENUE_TAGS:
        s = (se.annual_series_as_of(facts, tag, as_of) if as_of
             else se.annual_series(facts, tag))
        if len(s) < years + 1:
            continue
        first, last = s[-(years + 1)], s[-1]
        if first.value <= 0 or last.value <= 0:
            continue
        try:
            days = (date.fromisoformat(last.end) - date.fromisoformat(first.end)).days
        except ValueError:
            continue
        if days <= 0:
            continue
        span = days / 365.25
        cagr = (last.value / first.value) ** (1 / span) - 1
        basis = "已披露口径" if as_of else "全部数据（含基准日之后才披露的）"
        return cagr, f"{first.end} → {last.end}（{span:.1f} 年，{tag}，{basis}）"
    return None, (f"截止 {as_of} 不足 {years + 1} 个已披露年度值"
                  if as_of else f"不足 {years + 1} 个年度值")


def ebitda_margin_of(facts: dict, end: str | None = None) -> tuple[float | None, str]:
    """EBITDA 率 = 推算 EBITDA / 营业收入。

    **分母口径要对齐**：两个数必须来自同一财年。用最近的年度期末。
    """
    if end is None:
        for tag in se._REVENUE_TAGS:
            s = se.annual_series(facts, tag)
            if s:
                end = s[-1].end
                break
    if end is None:
        return None, "找不到年度收入数据"

    rev = se.derive_revenue(facts, end)
    eb = se.derive_ebitda(facts, end)
    if rev is None:
        return None, f"缺营业收入（期末 {end}）"
    if eb is None:
        return None, f"缺 EBITDA（期末 {end}）—— 通常缺折旧摊销科目"
    if rev.value <= 0:
        return None, f"营业收入非正（{rev.value:,.0f}），率没有意义"
    return eb.value / rev.value, f"期末 {end}：EBITDA {eb.value:,.0f} / 收入 {rev.value:,.0f}"


def revenue_scale(facts: dict) -> tuple[float | None, str]:
    for tag in se._REVENUE_TAGS:
        s = se.annual_series(facts, tag)
        if s:
            return s[-1].value, f"期末 {s[-1].end}（{tag}）"
    return None, "找不到年度收入"


METRIC_FUNCS = {
    "revenue_cagr": (revenue_cagr, "近三年收入 CAGR", ".2%"),
    "ebitda_margin": (ebitda_margin_of, "EBITDA 率", ".1%"),
    "revenue_scale": (revenue_scale, "营业收入规模", ",.0f"),
}


def build_peer_stat(
    peers: list[tuple[str, str]],
    metric: str = "revenue_cagr",
    years: int = 3,
    as_of: str | None = None,
) -> PeerStat:
    """从一组可比公司建分布。

    peers 是 [(cik, 名称), …]。名称只是给人看，CIK 才是取数的键。

    `as_of` 给了就只用那天已经披露的财报 —— **和 comps 模块同一套口径**。

    **拿不到就是拿不到** —— 每家算不出来的原因都记进 gaps，
    不静默丢弃，也不拿行业均值去填。
    """
    if metric not in METRIC_FUNCS:
        raise ValueError(f"不认识的指标 {metric!r}，可用：{'、'.join(METRIC_FUNCS)}")
    func, label, fmt = METRIC_FUNCS[metric]

    values: list[float] = []
    labels: list[str] = []
    gaps: list[str] = []

    for cik, name in peers:
        try:
            facts = se.company_facts(cik)
        except Exception as e:  # noqa: BLE001
            gaps.append(f"{name}：取数失败（{type(e).__name__}）")
            continue
        if metric == "revenue_cagr":
            val, why = func(facts, years, as_of=as_of)
        else:
            val, why = func(facts)
        if val is None:
            gaps.append(f"{name}：{why}")
            continue
        values.append(val)
        labels.append(name)

    basis = f"｜截止 {as_of} 已披露" if as_of else ""
    return PeerStat(
        metric=label, values=values, labels=labels, unit=fmt,
        source=f"SEC EDGAR XBRL（上市公司官方披露）{basis}", gaps=gaps,
    )


# ---------------------------------------------------------------------------
# 证据清单 —— 这部分是知识，不是猜测
# ---------------------------------------------------------------------------

@dataclass
class EvidenceItem:
    """一条能支撑或推翻假设的证据。"""

    level: str        # A / B / C，沿用 evidence-rules.md 的分级
    what: str         # 要什么
    where: str        # 去哪找
    why: str = ""     # 为什么它能支撑/推翻

    def render(self) -> str:
        line = f"[{self.level}] {self.what}"
        line += f"\n        去哪找：{self.where}"
        if self.why:
            line += f"\n        为什么：{self.why}"
        return line


#: 每类假设需要什么证据。
#
# **这是尽调的通行做法，写在代码里可以被审阅和修改。**
# 不放进模型生成 —— 同一个假设问两次应该拿到同一份清单，
# 而且清单本身是专业判断，不该随采样波动。
EVIDENCE_LIBRARY: dict[str, list[EvidenceItem]] = {
    "revenue_growth": [
        EvidenceItem("A", "已签合同/在手订单（含金额与交付期）", "销售合同、订单台账",
                     "在手订单是未来收入里唯一已经确定的部分"),
        EvidenceItem("A", "历史「量 × 价」拆解（至少三年）", "分产品销量表、销售明细",
                     "**增长来自放量还是提价，结论完全不同**。价驱动的增长依赖定价权，"
                     "通常不可持续；量驱动才可能延续"),
        EvidenceItem("A", "产能与产能利用率", "设备清单、三班排产记录、环评批复",
                     "产能上限决定增长的天花板，产能利用率说明还有多少余量"),
        EvidenceItem("B", "客户数量、新增客户、留存率", "CRM 导出、开票明细",
                     "新增客户贡献 vs 老客户复购，决定增长的稳定性"),
        EvidenceItem("A", "主要客户的实际采购计划或框架协议", "客户访谈、供应商名录",
                     "大客户占比高时，增长基本由它的采购计划决定"),
        EvidenceItem("B", "行业出货量/价格指数的外部数据", "行业协会、海关数据、统计局",
                     "用于判断公司增速相对行业是超越还是跟随"),
        EvidenceItem("C", "BP 中的市场空间测算", "商业计划书",
                     "市场空间大不等于公司能拿到 —— 只能形成假设，不能作为结论"),
        EvidenceItem("C", "管理层给出的收入目标", "访谈纪要、预算表",
                     "需先看其历史预测的达成率（见「历史达成率」一节）"),
    ],
    "ebitda_margin": [
        EvidenceItem("A", "分项成本结构（材料/人工/制造费用/期间费用）", "审计报告附注、成本明细表",
                     "固定成本与变动成本的比例，决定收入变化时利润率往哪走"),
        EvidenceItem("A", "主要原材料采购合同与历史价格", "采购合同、供应商对账单",
                     "**原材料价格能不能传导给客户，是利润率的核心变量**"),
        EvidenceItem("A", "加回项的原始凭证与性质", "审计调整分录、费用明细",
                     "**一次性收益和经常性收益要分开。** 把一次性开支的加回"
                     "当成长期利润率，是最常见的估值错误之一"),
        EvidenceItem("B", "单位成本随产量的变化（规模效应证据）", "成本核算表、分年度单位成本",
                     "规模效应是利润率提升假设的唯一硬依据"),
        EvidenceItem("B", "人工成本趋势与社保合规情况", "工资表、社保缴纳记录",
                     "未足额缴纳社保的中小企业，规范化后利润率会下降"),
        EvidenceItem("B", "同行业上市公司的利润率分布", "本工具自动取（EDGAR）",
                     "用于判断这个利润率在行业里是偏高还是偏低"),
    ],
    "wacc": [
        EvidenceItem("A", "银行授信函/贷款合同的实际利率与期限", "授信协议、借款合同",
                     "**债务成本用真实融资成本，不要用「行业平均」**"),
        EvidenceItem("A", "现有资本结构（有息负债明细、股权构成）", "审计报告、工商登记",
                     "决定加权时的权重。注意是当前的还是目标的资本结构"),
        EvidenceItem("B", "可比上市公司的去杠杆 beta", "本工具自动取（EDGAR + 行情）",
                     "非上市标的的 beta 必须由可比公司去杠杆后再按自身资本结构加杠杆"),
        EvidenceItem("B", "同信用等级债券的到期收益率", "中债估值、交易所披露",
                     "用于交叉验证债务成本是否合理"),
        EvidenceItem("A", "无风险利率", "国债收益率曲线（官方）",
                     "用与预测期匹配的期限，不要一律用十年期"),
        EvidenceItem("B", "股权风险溢价", "权威机构（如 Damodaran）定期发布",
                     "注意国别风险溢价。中国市场的 ERP 与成熟市场不同"),
    ],
    "terminal_growth": [
        EvidenceItem("A", "长期通胀预期", "央行/官方预测、TIPS 盈亏平衡通胀",
                     "**永续增长的硬上界是长期名义 GDP 增速** —— 一家公司"
                     "永远比整个经济长得快，在数学上不成立"),
        EvidenceItem("B", "行业长期增速的权威预测", "社科院、OECD、IMF 的长期展望",
                     "成熟行业的长期增速通常低于整体经济增速"),
        EvidenceItem("B", "标的自身的定价权证据", "提价历史、客户转换成本",
                     "只有具备定价权的公司，长期增速才可能不低于通胀"),
    ],
}

#: 假设名称里的关键词 → 证据清单的键。
#
# **顺序有意义，不能随意调。** 用 dict 的插入顺序做匹配优先级，
# 具体的类型必须排在泛化的类型前面 ——
# 「永续增长率」既含「永续」也含「增长率」，
# 如果 revenue_growth 在前，它会被错误归成收入增长假设。
_KIND_KEYWORDS = {
    "terminal_growth": ("永续", "终值", "terminal", "长期增长"),
    "wacc": ("wacc", "折现率", "资本成本", "cost of capital"),
    "ebitda_margin": ("ebitda率", "ebitda 率", "利润率", "毛利率", "margin", "加回"),
    "revenue_growth": ("收入增长", "营收增长", "增速", "增长率", "cagr", "增长假设"),
}


def classify(name: str) -> str | None:
    low = name.lower()
    for kind, words in _KIND_KEYWORDS.items():
        if any(w in low for w in words):
            return kind
    return None


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

@dataclass
class Advice:
    """对一个假设的对照意见。"""

    assumption: Assumption
    kind: str | None
    peer: PeerStat | None
    percentile: float | None
    evidence: list[EvidenceItem]
    gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        out = [f"假设：{self.assumption.name} = {self.assumption.value}"
               f"{self.assumption.unit}   来源：{self.assumption.source}"]

        out.append("")
        out.append("对照：")
        if self.peer is None:
            out.append("  · 没有可用的同行分布 —— **不给你凑一个参照系。**"
                       "硬凑的参照系比没有更糟：它会让你以为自己有了依据。")
        elif not self.peer.values:
            out.append("  · 同行分布为空（取数失败或样本不足）")
        else:
            for line in self.peer.render(self.peer.unit or ".2%").splitlines():
                out.append(f"  {line}")
            if self.percentile is not None and self.percentile == self.percentile:
                pct = self.percentile
                out.append(f"    → 你的假设落在第 {pct:.0%} 分位"
                           f"（比 {pct:.0%} 的同行高）")
                if pct >= 0.75:
                    out.append("    → **高于 P75。** 需要明确说明凭什么能超过"
                               "四分之三的同行 —— 这是最该被追问的地方。")
                elif pct <= 0.25:
                    out.append("    → 低于 P25。若标的确实在衰退，这个假设合理；"
                               "否则可能低估了。")
            out.append(f"  来源：{self.peer.source}")

        if self.peer is not None and self.peer.gaps:
            out.append("")
            out.append("  未能计入样本的：")
            for g in self.peer.gaps[:8]:
                out.append(f"    ✗ {g}")
            if len(self.peer.gaps) > 8:
                out.append(f"    …还有 {len(self.peer.gaps) - 8} 家")

        if self.evidence:
            out.append("")
            out.append("支撑这个假设需要什么证据：")
            for item in self.evidence:
                out.append("  " + item.render().replace("\n", "\n  "))

        if self.gaps:
            out.append("")
            out.append("数据缺口：")
            for g in self.gaps:
                out.append(f"  ✗ {g}")

        if self.notes:
            out.append("")
            for n in self.notes:
                out.append(f"  · {n}")

        return "\n".join(out)


def advise(
    assumption: Assumption,
    peers: list[tuple[str, str]] | None = None,
    metric: str = "revenue_cagr",
    years: int = 3,
    as_of: str | None = None,
    peer_stat: PeerStat | None = None,
) -> Advice:
    """给一个假设配对照系。

    peers 给了就现算同行分布；也可以直接传算好的 peer_stat（复用，少调接口）。

    **假设本身就是 C 级证据**（管理层口径或用户估计），除非它的来源是
    A 级材料 —— 这一点会写进输出，因为它决定了这个假设该被追问到什么程度。
    """
    kind = classify(assumption.name)
    gaps: list[str] = []
    notes: list[str] = []

    stat = peer_stat
    if stat is None and peers:
        stat = build_peer_stat(peers, metric=metric, years=years, as_of=as_of)

    pct = None
    if stat is not None and stat.values and assumption.value is not None:
        pct = stat.percentile_of(assumption.value)

    if kind is None:
        notes.append(
            "没能把这个假设归类到已知类型，所以给不出专门的证据清单。"
            "**这不是说它不重要** —— 只是本工具的清单库还没覆盖到。"
            "算出来的同行分布仍然可用。"
        )
    elif not EVIDENCE_LIBRARY.get(kind):
        notes.append(f"类型 {kind} 的证据清单还是空的")

    # 假设自身的证据级别
    if assumption.confidence is Confidence.HIGH:
        notes.append("这个假设的来源是 A 级材料（审计/合同/官方披露），"
                     "可以直接作为结论依据。")
    elif assumption.confidence is Confidence.MISSING:
        gaps.append(f"{assumption.name} 没有值，无从对照")
    else:
        notes.append(
            f"这个假设的来源「{assumption.source}」属于 "
            f"{'B 级（需交叉验证）' if assumption.confidence is Confidence.MEDIUM else 'C 级（只能形成假设，不能作为结论）'}。"
            "**照它算出来的估值，结论强度不能超过这个假设本身的强度。**"
        )

    return Advice(
        assumption=assumption, kind=kind, peer=stat, percentile=pct,
        evidence=list(EVIDENCE_LIBRARY.get(kind, [])) if kind else [],
        gaps=gaps, notes=notes,
    )


def terminal_growth_check(
    g: Assumption,
    long_term_nominal_gdp: Assumption,
    long_term_inflation: Assumption | None = None,
) -> list[str]:
    """永续增长的硬边界检查。

    **一家公司永远比整个经济长得快，在数学上不成立。**
    所以 g 不能超过长期名义 GDP 增速 —— 这是边界条件，不是观点。

    实际项目里最常见的错误：拍一个 3–4% 的永续增长，
    却没意识到「3% + 通胀」已经顶到长期名义 GDP 了。
    """
    problems: list[str] = []
    gv = g.value
    gdp = long_term_nominal_gdp.value
    if gv is None or gdp is None:
        return ["永续增长或长期名义 GDP 增速缺失，无法做边界检查"]

    if gv > gdp:
        problems.append(
            f"永续增长 {gv:.2%} **超过了长期名义 GDP 增速 {gdp:.2%}**。"
            f"这意味着标的永远比整个经济长得快 —— 数学上不成立。"
        )
    elif gv > gdp * 0.9:
        problems.append(
            f"永续增长 {gv:.2%} 已接近长期名义 GDP 增速 {gdp:.2%} 的上限。"
            f"要继续用这个假设，需要说明标的有怎样的持续定价权。"
        )

    if long_term_inflation is not None and long_term_inflation.value is not None:
        infl = long_term_inflation.value
        if gv < infl:
            problems.append(
                f"永续增长 {gv:.2%} 低于长期通胀预期 {infl:.2%}。"
                f"这等于假设公司长期实际在萎缩 —— 如果真是这样，"
                f"终值可能应该更低，或者干脆不该用永续增长法。"
            )
    return problems
