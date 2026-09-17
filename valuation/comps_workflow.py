"""可比公司选取 —— **流程层**（`comps.py` 只做机械计算）。

## 用户的原话（2026-09-17）

> 可比公司倍数，需要网络搜索的，需要根据被分析公司的行业进行搜索，
> 需要**建议用户**该公司应该对标哪些行业，然后从**地域、公司规模、
> 融资轮次、商业模式**等等选取对标公司，同时对标的**相应指标也需要跟用户
> 进行讨论确定**。对标**至少 3 家**。**基准日还是要跟分析公司响应时间对应**。

## 这个模块管什么

    comps.py         给一家公司，算它的市值 / EV / 倍数      （机械）
    comps_workflow.py 决定「该拿哪几家公司来比、比哪些指标」  （判断）

后者是**判断**，所以每一步都要用户拍板，程序只负责**把选项和依据摆出来**。

## 四条纪律

**一、查询词里绝不能带标的名称。**
搜索引擎是公网。把「XX 公司 可比公司」发出去，等于把 deal 名单公开。
所以查询只用**行业 + 结构化条件**，并且在发出去之前**断言标的名称不在里面**。
项目里已有 `root/privacy-blocklist.txt` 禁名表 。

**二、基准日必须和被分析公司的报表期间对齐。**
拿 2026 年的可比公司倍数去比 2022 年的标的，是前视偏差 ——
**用了当时还不存在的信息**。这条和 `comps.py` 里「筛选只能用基准日前已披露的财报」
是同一条纪律的两面。

**三、至少 3 家，不够就明说不够。**
2 家算出来的「中位数」没有意义。样本不足时**拒绝给区间**，
而不是拿 2 家凑一个数出来。

**四、指标要用户确认，不代填。**
用 EV/EBITDA 还是 EV/收入、要不要扣掉净债务 —— 这些是估值判断。
程序列出候选指标和各自的适用条件，由用户选。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 少于这个数就不给区间
MIN_COMPS = 3

#: 可以用来对标的主要指标 —— 每一项都带「什么时候适用 / 什么时候不适用」
METRIC_CHOICES: tuple[dict[str, str], ...] = (
    {"name": "EV/EBITDA",
     "use": "有稳定正 EBITDA、资本结构差异大（要剔除杠杆影响）",
     "avoid": "重资产折旧差异极大、或 EBITDA 为负"},
    {"name": "EV/EBIT",
     "use": "折旧政策差异大、或需要反映资本开支强度",
     "avoid": "EBIT 为负"},
    {"name": "EV/Revenue",
     "use": "尚未盈利 / SaaS 等以收入为锚的商业模式",
     "avoid": "毛利率差异大 —— 收入倍数对毛利率极其敏感"},
    {"name": "P/E",
     "use": "盈利稳定、杠杆水平接近",
     "avoid": "亏损、或一次性损益大"},
    {"name": "P/B",
     "use": "金融业、或资产是主要价值来源",
     "avoid": "轻资产、无形资产不入账（品牌、技术）"},
    {"name": "EV/ARR",
     "use": "订阅制 / SaaS",
     "avoid": "非经常性收入占比高"},
)


@dataclass
class ScreeningCriteria:
    """筛选条件。**每一项都要跟用户确认，程序只给建议值。**"""

    industries: list[str] = field(default_factory=list)
    #: 地域 —— 影响倍数系统性差异（新兴市场折价）
    geography: str = ""
    #: 用什么衡量规模
    size_metric: str = "营业收入"
    size_min: float | None = None
    size_max: float | None = None
    #: 融资轮次（一级市场 / 未上市公司才用）
    funding_stage: str = ""
    #: 商业模式 —— 同一行业里 to B / to C 的倍数能差一倍
    business_model: str = ""
    #: 是否只取上市公司（上市公司才有连续公开报价）
    listed_only: bool = True

    def render(self) -> str:
        parts = [f"行业：{'、'.join(self.industries) or '**未定**'}"]
        if self.geography:
            parts.append(f"地域：{self.geography}")
        if self.size_min is not None or self.size_max is not None:
            lo = f"{self.size_min:,.0f}" if self.size_min is not None else "—"
            hi = f"{self.size_max:,.0f}" if self.size_max is not None else "—"
            parts.append(f"{self.size_metric}：{lo} – {hi}")
        if self.funding_stage:
            parts.append(f"轮次：{self.funding_stage}")
        if self.business_model:
            parts.append(f"商业模式：{self.business_model}")
        if self.listed_only:
            parts.append("只取上市公司")
        return "  · " + "\n  · ".join(parts)


@dataclass
class Candidate:
    """一家候选可比公司。**用户逐家确认。**"""

    name: str
    industry: str = ""
    geography: str = ""
    size: float | None = None
    funding_stage: str = ""
    business_model: str = ""
    #: 为什么算可比 —— 一句话，**不写清楚就不该进候选**
    why: str = ""
    source: str = ""
    #: None = 待用户定；True / False = 用户已表态
    accepted: bool | None = None
    #: 用户否掉的原因 / 需要补的数据
    note: str = ""


@dataclass
class CompsSelection:
    target_industry: str = ""
    criteria: ScreeningCriteria = field(default_factory=ScreeningCriteria)
    candidates: list[Candidate] = field(default_factory=list)
    #: 拿去算倍数的指标（用户确认过的）
    metrics: list[str] = field(default_factory=list)
    #: 基准日 —— **必须和被分析公司的报表期间对齐**
    as_of: str = ""
    target_period: str = ""
    notes: list[str] = field(default_factory=list)

    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates if c.accepted]

    def ready(self) -> bool:
        return len(self.accepted()) >= MIN_COMPS and bool(self.metrics) and bool(self.as_of)

    def check(self) -> list[str]:
        """还差什么才能算。**每一条都是「不能开始」的理由，不是提醒。**"""
        out = []
        if not self.criteria.industries:
            out.append("**行业还没定** —— 要先跟用户确认对标哪些行业")
        if not self.accepted():
            out.append("**一家可比公司都还没确认**")
        elif len(self.accepted()) < MIN_COMPS:
            out.append(
                f"**只确认了 {len(self.accepted())} 家，不够 {MIN_COMPS} 家。**"
                f"样本不足时算出来的「中位数」没有意义 —— "
                f"这种情况要拒绝给区间，不要拿两三家凑一个数。")
        if not self.metrics:
            out.append("**对标指标还没定** —— EV/EBITDA 还是 EV/收入，是估值判断，"
                       "要用户拍板")
        if not self.as_of:
            out.append("**基准日没给** —— 可比公司的倍数取哪一天，必须显式指定")
        elif self.target_period and self.as_of < self.target_period:
            out.append(
                f"⚠ **基准日（{self.as_of}）早于标的报表期间（{self.target_period}）。**"
                f"这样算出来的倍数比标的还旧，比出来的数会系统性偏低。")
        return out


# ---------------------------------------------------------------------------
# 查询构造 —— 这一节是保密的落点
# ---------------------------------------------------------------------------

#: 查询里**只允许**出现这类词
_SAFE_TOKEN = re.compile(r"^[\w\u4e00-\u9fff\s\-&/]+$")


def build_queries(sel: CompsSelection, blocked_names: list[str]) -> list[str]:
    """把筛选条件翻成搜索查询。

    ## 这是整个流程最需要小心的一步

    搜索是在**公网**上跑的。把「XX 公司 可比公司」发出去，
    等于把 deal 名单公开 —— 项目里已有「标的公司名是机密」这条纪律。

    所以：
    1. 查询**只用行业 + 结构化条件**（地域、规模、上市地）
    2. 发出去之前**逐条断言**禁名表里的名字没混进去
    3. 断言失败就**抛异常**，不是静默过滤 —— 静默过滤会让人以为查过了

    ## 为什么是抛异常而不是自动删掉

    如果标的名称不小心进了查询词，那说明**上游把机密内容传下来了**。
    自动删掉会让这个 bug 沉下去，下次换个字段又漏一遍。
    """
    qs: list[str] = []
    for ind in sel.criteria.industries:
        bits = [ind]
        if sel.criteria.geography:
            bits.append(sel.criteria.geography)
        if sel.criteria.listed_only:
            bits.append("上市公司")
        bits.append("可比公司")
        qs.append(" ".join(bits))

    for q in qs:
        _assert_no_blocked(q, blocked_names)
    return qs


def _assert_no_blocked(query: str, blocked_names: list[str]) -> None:
    low = query.lower()
    for name in blocked_names:
        n = (name or "").strip()
        if len(n) >= 2 and n.lower() in low:
            raise ValueError(
                f"**查询词里出现了禁名表里的名字「{n}」，已中止。**\n"
                f"  查询：{query}\n"
                f"  搜索是在公网上跑的，发出去等于公开 deal 名单。\n"
                f"  这通常说明上游把标的名称传进了筛选条件 —— "
                f"**要修的是上游，不是在这里偷偷把词删掉。**")


def load_blocklist(path) -> list[str]:
    """读禁名表。没有就返回空表 —— 但调用方要知道**空表等于没保护**。"""
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


# ---------------------------------------------------------------------------
# 渲染：给用户看的确认清单
# ---------------------------------------------------------------------------

def render_worklist(sel: CompsSelection) -> str:
    """把「还差什么」摆给用户 —— 这是互动环节的输入。"""
    out = [f"  可比公司选取　基准日 {sel.as_of or '**未定**'}"
           f"　标的期间 {sel.target_period or '**未定**'}"]
    out.append(sel.criteria.render())
    out.append("")
    if sel.candidates:
        out.append("  候选（待确认）：")
        for c in sel.candidates:
            mark = {True: "✓", False: "✗", None: "·"}[c.accepted]
            out.append(f"    {mark} {c.name:22} {c.industry:12}"
                       f"{c.geography:8}{c.why[:40]}")
    else:
        out.append("  候选：**还没有** —— 要先用行业 + 条件去搜")
    out.append("")
    out.append(f"  已确认 {len(sel.accepted())} 家 / 至少 {MIN_COMPS} 家")
    if sel.metrics:
        out.append(f"  指标：{'、'.join(sel.metrics)}")
    else:
        out.append("  指标：**未定**")
    gaps = sel.check()
    if gaps:
        out.append("")
        out.append("  **还不能开始算，缺：**")
        for g in gaps:
            out.append(f"    · {g}")
    for n in sel.notes:
        out.append(f"  · {n}")
    return "\n".join(out)


@dataclass
class IndustryProposal:
    """一条行业建议。**必须带依据** —— 用户要能看出它是从哪句话推出来的。"""

    industry: str
    #: 命中了业务描述里的哪些词
    evidence: list[str] = field(default_factory=list)
    #: 为什么建议对标这个行业
    why: str = ""
    #: 这个行业里找可比公司要注意什么
    caveat: str = ""


#: 行业关键词表 —— 从业务描述里认行业。
#:
#: ⚠️ **这只是「建议」，不是自动选定。** 用户要能看到了依据再拍板 ——
#: 对哪个行业直接影响倍数取值，猜错了后面全错。
_INDUSTRY_HINTS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("营销服务 / 广告代理",
     ("营销", "广告", "公关", "公共关系", "传播", "创意", "投放", "媒介"),
     "业务描述里的核心词直接落在营销传播上",
     "轻资产、人力密集，**EV/EBITDA 比 P/E 稳**；但收入里「媒介代理」"
     "过手金额大、毛利极薄，要区分「收入口径」和「净收入口径」"),
    ("营销科技 / MarTech",
     ("营销科技", "数字营销", "程序化", "DSP", "数据驱动", "智能投放"),
     "带有技术属性的营销服务，倍数通常高于传统广告代理",
     "要看技术收入占比 —— 挂个「科技」名字但实质是代理的，倍数不该按 SaaS 给"),
    ("出海营销",
     ("出海", "海外", "跨境", "全球主要市场", "Meta", "Google", "TikTok"),
     "有明确的海外投放业务，是独立且增长更快的一档",
     "**地域要对齐** —— 出海业务的倍数锚在海外同业，不是 A 股同业"),
    ("企业服务 / to B 服务",
     ("赋能", "解决方案", "服务商", "企业客户", "智慧经营"),
     "面向企业提供解决方案，商业模式是 to B",
     "to B 与 to C 的倍数能差一倍，**筛选时商业模式要单独拎出来**"),
    ("人力资源服务",
     ("人力资源", "猎头", "招聘", "外包", "劳务派遣", "薪酬代发", "EOR"),
     "业务描述落在人力资源服务上",
     "人力外包的毛利率极低（个位数），**EV/Revenue 会严重失真**，"
     "优先用 EV/EBITDA 或 EV/毛利"),
    ("软件 / SaaS",
     ("SaaS", "订阅", "软件", "平台化", "云服务"),
     "有订阅或软件产品特征",
     "用 **EV/ARR 而不是 EV/Revenue** —— ARR 剔除了实施和一次性收入"),
    ("制造业",
     ("制造", "生产", "产能", "生产线", "工厂"),
     "有实体生产环节",
     "**CapEx 和折旧差异极大**，EV/EBITDA 前要先看折旧政策是否可比"),
    ("新能源 / 电池",
     ("新能源", "电池", "储能", "光伏", "风电", "锂"),
     "业务描述落在新能源产业链上",
     "**产能周期**影响大，可比公司要选同一产能周期阶段的"),
    ("矿业 / 资源",
     ("矿", "采选", "冶炼", "资源", "储量"),
     "有资源开采或冶炼业务",
     "**商品价格周期**主导业绩，可比公司要选同期同品种的"),
    ("消费 / 食品饮料",
     ("白酒", "食品", "饮料", "消费品", "品牌"),
     "有品牌消费品业务",
     "**品牌溢价**不可比 —— 高端品牌和大众品牌倍数差几倍，先按价格带分层"),
    ("教育",
     ("教育", "培训", "课程", "院校"),
     "有教育服务业务",
     "**政策敏感**，政策窗口不同期的倍数不可比"),
    ("金融",
     ("银行", "保险", "证券", "信托", "租赁", "金融"),
     "有金融牌照业务",
     "**用 P/B 不用 EV/EBITDA** —— 金融企业的负债是经营资产，不是融资安排"),
)


def propose_industries(business_text: str, *, max_n: int = 4) -> list[IndustryProposal]:
    """从业务描述里提对标行业建议。**提建议，不替用户定。**

    ## 为什么必须带依据

    对哪个行业直接决定倍数取值 —— 猜错了后面全错。
    所以每条建议都回带「命中了业务描述里的哪些词」，
    用户扫一眼就知道它是不是在胡猜。

    ## 为什么要给 `caveat`

    每个行业都有**最容易比错的地方**。光说「对标营销服务」不够 ——
    还得说「营销服务的收入里媒介代理过手金额大、毛利极薄，
    要区分收入口径和净收入口径」。不然用户拿着一堆看起来可比的倍数，
    比出来的东西是错的。
    """
    t = (business_text or "").lower()
    if not t:
        return []

    out: list[IndustryProposal] = []
    for industry, keys, why, caveat in _INDUSTRY_HINTS:
        hits = [k for k in keys if k.lower() in t]
        if hits:
            out.append(IndustryProposal(industry=industry, evidence=hits,
                                        why=why, caveat=caveat))
    # 命中的关键词越多越相关
    out.sort(key=lambda p: -len(p.evidence))
    return out[:max_n]


def render_industry_proposal(props: list[IndustryProposal]) -> str:
    """给用户看的行业建议 —— **这是互动环节的输入**。"""
    if not props:
        return ("  **提不出行业建议** —— 材料里没有可识别的业务描述。\n"
                "    要找年报的「公司从事的主要业务」或招股书的「主营业务」一节。")
    out = ["  建议对标以下行业（**请确认或修改**，程序不替你选）："]
    for i, p in enumerate(props, 1):
        out.append(f"    {i}. {p.industry}")
        out.append(f"       依据：业务描述里出现「{'、'.join(p.evidence[:6])}」")
        out.append(f"       理由：{p.why}")
        out.append(f"       ⚠ 对标时注意：{p.caveat}")
    return "\n".join(out)


def metric_menu() -> str:
    """指标候选清单 —— 给用户挑，附各自的适用/不适用条件。"""
    out = ["  备选指标（要用户确认，程序不代填）："]
    for m in METRIC_CHOICES:
        out.append(f"    {m['name']:12} 适用：{m['use']}")
        out.append(f"    {'':12} 不适合：{m['avoid']}")
    return "\n".join(out)
