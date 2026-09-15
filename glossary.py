"""中英术语表 —— 让中文问题和英文材料能互相找到。

## 为什么用术语表，而不是翻译

跨语言检索有三条路：

| 做法 | 问题 |
|---|---|
| 拿本地大模型把查询翻译过去 | 每次结果可能不一样；多一次推理（qwen3:14b 要 70 秒）；**不可审阅** |
| 入口时把整份材料翻译一遍 | 921 个片段要逐个翻；译文质量被永久冻结；材料一变就得重来 |
| **术语表扩展查询** | 覆盖的是**封闭词汇集** |

选第三条，因为这个场景有个特殊条件：**财务术语是数得清的**。
「营业收入 / revenue」「应收账款 / accounts receivable」「毛利率 / gross margin」——
不像自由文本那样无边无际。

好处是确定、可审阅、零依赖、零延迟。术语表就摆在仓库里，
开源用户可以自己加自己行业的词。

## 双向的

```
中文提问 + 英文材料  →  查询里附上英文术语
英文提问 + 中文材料  →  查询里附上中文术语
```

同一个表两个方向都用，不用维护两份。

## 为什么不只加"词"，还加 XBRL 标签

索引里的表格标题带了官方标签（`us-gaap:Assets`，见 `ingest/html.py`）。
所以「总资产」如果能扩展出 `Assets`，就能直接命中那条表头。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Term:
    zh: str
    en: str
    #: 同类写法的其他说法（两个方向都会用上）
    alt: tuple[str, ...] = ()


#: 术语表。**优先放长的、具体的词** ——
#: 「营业利润」必须排在「利润」前面，否则「利润」会先把位置占了。
GLOSSARY: tuple[Term, ...] = (
    # ---------------- 三张表与报告 ----------------
    Term("资产负债表", "balance sheet", ("statement of financial position",)),
    Term("利润表", "income statement", ("statement of operations",)),
    Term("现金流量表", "cash flow statement", ("statement of cash flows",)),
    Term("合并报表", "consolidated financial statements", ("consolidated",)),
    Term("财务报表附注", "notes to financial statements", ("notes to consolidated",)),
    Term("年报", "annual report", ("10-K", "Form 10-K")),
    Term("季报", "quarterly report", ("10-Q", "Form 10-Q")),
    Term("审计报告", "auditor's report", ("audit report",)),
    Term("未经审计", "unaudited", ()),
    Term("管理层讨论", "management's discussion and analysis", ("MD&A",)),
    Term("财务报表", "financial statements", ("financials",)),

    # ---------------- 资产负债表 ----------------
    Term("货币资金", "cash and cash equivalents", ("cash",)),
    Term("交易性金融资产", "marketable securities", ("short-term investments",)),
    Term("应收账款", "accounts receivable", ("receivables",)),
    Term("应收票据", "notes receivable", ()),
    Term("存货", "inventories", ("inventory",)),
    Term("预付账款", "prepaid expenses", ("prepaid",)),
    Term("流动资产", "current assets", ()),
    Term("固定资产", "property and equipment", ("fixed assets", "PP&E")),
    Term("在建工程", "construction in progress", ()),
    Term("无形资产", "intangible assets", ("intangible",)),
    Term("商誉", "goodwill", ()),
    Term("递延所得税资产", "deferred tax assets", ()),
    Term("总资产", "total assets", ("Assets",)),
    Term("应付账款", "accounts payable", ("payables",)),
    Term("预收账款", "deferred revenue", ("advance payments",)),
    Term("应付职工薪酬", "accrued compensation", ("employee benefits payable",)),
    Term("应交税费", "income taxes payable", ("taxes payable",)),
    Term("短期借款", "short-term borrowings", ("short-term debt",)),
    Term("其他应付款", "accrued liabilities", ("other payables",)),
    Term("流动负债", "current liabilities", ()),
    Term("长期借款", "long-term debt", ("long-term borrowings",)),
    Term("长期应付款", "other liabilities", ()),
    Term("递延所得税负债", "deferred tax liabilities", ()),
    Term("总负债", "total liabilities", ("Liabilities",)),
    Term("有息负债", "interest-bearing debt", ("total debt",)),
    Term("实收资本", "common stock", ("share capital",)),
    Term("资本公积", "additional paid-in capital", ("capital reserve",)),
    Term("留存收益", "retained earnings", ("retained profits",)),
    Term("其他综合收益", "accumulated other comprehensive income", ("OCI",)),
    Term("所有者权益", "stockholders' equity", ("shareholders' equity", "StockholdersEquity")),
    Term("归属母公司股东的权益", "equity attributable to the parent", ()),
    Term("少数股东权益", "noncontrolling interests", ("minority interests",)),
    Term("营运资本", "working capital", ()),
    Term("净营运资本", "net working capital", ()),

    # ---------------- 利润表 ----------------
    Term("营业收入", "revenue", ("revenues", "net sales", "turnover")),
    Term("主营业务收入", "operating revenue", ()),
    Term("营业成本", "cost of revenue", ("cost of sales", "COGS")),
    Term("毛利", "gross profit", ()),
    # 泛化的「利润」放在具体科目之后不影响匹配（长词优先），
    # 但用户单独问「利润」时要能扩展。
    Term("利润", "profit", ("income",)),
    Term("毛利率", "gross margin", ("gross profit margin",)),
    Term("营业费用", "operating expenses", ("operating expense",)),
    Term("销售费用", "selling expenses", ("sales and marketing",)),
    Term("管理费用", "general and administrative expenses", ("G&A",)),
    Term("研发费用", "research and development expenses", ("R&D",)),
    Term("营业利润", "operating income", ("operating profit", "income from operations")),
    Term("息税前利润", "EBIT", ("earnings before interest and taxes",)),
    Term("息税折旧摊销前利润", "EBITDA", ()),
    Term("折旧", "depreciation", ("depreciation and amortization", "D&A")),
    Term("摊销", "amortization", ()),
    Term("利息费用", "interest expense", ()),
    Term("利息收入", "interest income", ()),
    Term("财务费用", "finance costs", ("financial expenses",)),
    Term("利润总额", "income before income taxes", ("pretax income", "pre-tax income")),
    Term("所得税", "income tax expense", ("provision for income taxes", "tax expense")),
    Term("净利润", "net income", ("net earnings", "net loss", "NetIncomeLoss")),
    Term("归母净利润", "net income attributable to the parent", ()),
    Term("净利润率", "net margin", ("净利率", "net profit margin")),
    Term("营业利润率", "operating margin", ()),
    Term("每股收益", "earnings per share", ("EPS",)),
    Term("基本每股收益", "basic earnings per share", ()),
    Term("稀释每股收益", "diluted earnings per share", ()),
    Term("股份支付", "stock-based compensation", ("share-based compensation",)),
    Term("非经常性损益", "non-recurring items", ("one-time charges",)),
    Term("加回项", "add-backs", ("addbacks",)),

    # ---------------- 现金流量表 ----------------
    Term("经营活动现金流", "cash flows from operating activities", ("operating cash flow", "OCF")),
    Term("投资活动现金流", "cash flows from investing activities", ("investing cash flow",)),
    Term("筹资活动现金流", "cash flows from financing activities", ("financing cash flow",)),
    Term("自由现金流", "free cash flow", ("FCF",)),
    Term("资本开支", "capital expenditures", ("capex", "purchases of property and equipment")),
    Term("经营性现金净流量", "net cash provided by operating activities", ()),
    Term("现金及现金等价物净增加额", "net increase in cash and cash equivalents", ()),
    Term("回购股份", "repurchase of common stock", ("share buyback",)),
    Term("分红", "dividends", ("dividend payments",)),

    # ---------------- 财务指标 ----------------
    Term("收入增长率", "revenue growth", ("revenue growth rate",)),
    Term("复合增长率", "compound annual growth rate", ("CAGR",)),
    Term("同比", "year over year", ("YoY",)),
    Term("环比", "quarter over quarter", ("QoQ",)),
    Term("毛利率变化", "gross margin change", ()),
    Term("费用率", "expense ratio", ()),
    Term("周转率", "turnover ratio", ()),
    Term("存货周转", "inventory turnover", ()),
    Term("应收账款周转", "receivables turnover", ()),
    Term("资产负债率", "debt-to-asset ratio", ()),
    Term("流动比率", "current ratio", ()),
    Term("速动比率", "quick ratio", ()),
    Term("净资产收益率", "return on equity", ("ROE",)),
    Term("总资产收益率", "return on assets", ("ROA",)),
    Term("投入资本回报率", "return on invested capital", ("ROIC",)),
    Term("客户集中度", "customer concentration", ()),
    Term("前五大客户", "top five customers", ()),

    # ---------------- 估值 ----------------
    Term("估值", "valuation", ()),
    Term("折现现金流", "discounted cash flow", ("DCF",)),
    Term("净现值", "net present value", ("NPV",)),
    Term("内部收益率", "internal rate of return", ("IRR",)),
    Term("加权平均资本成本", "weighted average cost of capital", ("WACC",)),
    Term("资本成本", "cost of capital", ()),
    Term("折现率", "discount rate", ()),
    Term("永续增长", "terminal growth", ("perpetual growth", "terminal growth rate")),
    Term("终值", "terminal value", ("TV",)),
    Term("退出倍数", "exit multiple", ()),
    Term("可比公司", "comparable companies", ("comps", "comparables", "peer group")),
    Term("整体估值倍数", "multiple", ("valuation multiple",)),
    Term("市盈率", "price-to-earnings ratio", ("P/E",)),
    Term("市净率", "price-to-book ratio", ("P/B",)),
    Term("企业价值", "enterprise value", ("EV",)),
    Term("股权价值", "equity value", ()),
    Term("投前估值", "pre-money valuation", ("pre-money",)),
    Term("投后估值", "post-money valuation", ("post-money",)),
    Term("敏感性分析", "sensitivity analysis", ()),
    Term("情景分析", "scenario analysis", ()),
    Term("协同效应", "synergies", ()),

    # ---------------- 交易与尽调 ----------------
    Term("尽职调查", "due diligence", ()),
    Term("尽调", "due diligence", ()),
    Term("投资意向书", "letter of intent", ("LOI",)),
    Term("条款清单", "term sheet", ()),
    Term("股权收购", "equity purchase", ("share purchase",)),
    Term("资产收购", "asset purchase", ()),
    Term("股权转让协议", "share purchase agreement", ("SPA",)),
    Term("对赌", "earnout", ("performance-based consideration", "contingent consideration")),
    Term("业绩承诺", "performance commitment", ("earnout",)),
    Term("回购条款", "redemption clause", ("repurchase obligation", "buyback right")),
    Term("优先清算权", "liquidation preference", ()),
    Term("反稀释", "anti-dilution", ()),
    Term("领售权", "drag-along right", ()),
    Term("随售权", "tag-along right", ()),
    Term("优先购买权", "right of first refusal", ("ROFR",)),
    Term("交割条件", "closing conditions", ()),
    Term("过渡期", "interim period", ()),
    Term("锁定期", "lock-up period", ()),
    Term("排他期", "exclusivity period", ()),

    # ---------------- 公司与治理 ----------------
    Term("实际控制人", "controlling shareholder", ("actual controller",)),
    Term("实控人", "controlling shareholder", ()),
    Term("董事长", "chairman", ("chairman of the board",)),
    Term("董事会", "board of directors", ()),
    Term("管理层", "management", ("management team",)),
    Term("创始人", "founder", ()),
    Term("员工人数", "number of employees", ("headcount",)),
    Term("子公司", "subsidiaries", ("subsidiary",)),
    Term("关联交易", "related party transactions", ("related-party",)),
    Term("股权结构", "ownership structure", ("cap table", "capitalization")),
    Term("普通股", "common stock", ("ordinary shares",)),
    Term("优先股", "preferred stock", ("preference shares",)),
    Term("创始人持股", "founder ownership", ()),
    Term("同业竞争", "competition with the company", ()),

    # ---------------- 风险与法律 ----------------
    Term("风险因素", "risk factors", ()),
    Term("诉讼", "litigation", ("legal proceedings", "lawsuit")),
    Term("重大诉讼", "material litigation", ()),
    Term("专利", "patents", ("intellectual property", "IP")),
    Term("知识产权", "intellectual property", ()),
    Term("商标", "trademarks", ()),
    Term("持续经营", "going concern", ()),
    Term("或有负债", "contingent liabilities", ()),
    Term("担保", "guarantees", ("guarantee",)),
    Term("抵押", "pledge", ("collateral", "mortgage")),
    Term("资产减值", "asset impairment", ("impairment",)),
    Term("监管审批", "regulatory approval", ()),
    Term("合规", "compliance", ()),
    Term("反垄断", "antitrust", ()),
    Term("外汇风险", "foreign exchange risk", ("FX risk",)),
    Term("客户流失", "customer attrition", ("churn",)),
    Term("供应链", "supply chain", ()),
    Term("单一供应商", "sole supplier", ("single-source supplier",)),
    Term("质保", "warranty", ("product warranty",)),
    Term("退换货", "returns and allowances", ("product returns",)),

    # ---------------- 业务与行业 ----------------
    Term("市场占有率", "market share", ()),
    Term("销售额", "sales", ()),
    Term("出货量", "units shipped", ("shipments",)),
    Term("平均售价", "average selling price", ("ASP",)),
    Term("毛利率驱动因素", "margin drivers", ()),
    Term("在手订单", "backlog", ("order book",)),
    Term("客户留存", "customer retention", ()),
    Term("复购率", "repeat purchase rate", ()),
    Term("渠道", "channel", ("distribution channel",)),
    Term("代工", "contract manufacturing", ("OEM",)),
    Term("产能利用率", "capacity utilization", ()),
    Term("单位经济模型", "unit economics", ()),
    Term("经常性收入", "recurring revenue", ()),
    Term("年度经常性收入", "annual recurring revenue", ("ARR",)),
    Term("月度经常性收入", "monthly recurring revenue", ("MRR",)),
    Term("客户获取成本", "customer acquisition cost", ("CAC",)),
    Term("客户终身价值", "customer lifetime value", ("LTV",)),
    Term("流失率", "churn rate", ()),
)


def _free(span: tuple[int, int], covered: list[tuple[int, int]]) -> bool:
    """这段位置还没被更长的词占掉。"""
    s, e = span
    return all(e <= cs or s >= ce for cs, ce in covered)


def _match_spans(query: str, text: str) -> list[tuple[int, int]]:
    """text 在 query 里出现的所有位置。"""
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        i = query.find(text, start)
        if i < 0:
            return spans
        spans.append((i, i + len(text)))
        start = i + 1


def expand_terms(query: str, max_added: int = 16) -> list[str]:
    """返回要追加的术语**列表**（不是拼好的字符串）。

    `expand_query` 用它。单独暴露出来是为了可测试 ——
    用字符串没法区分「独立加进来的 `income`」和「`operating income` 里的一部分」，
    而这两者含义完全不同。按列表判断就没有歧义。
    """
    if not query.strip():
        return []

    q_lower = query.lower()
    covered: list[tuple[int, int]] = []
    added: list[str] = []

    def _already_present(word: str) -> bool:
        return word.lower() in q_lower or any(word.lower() == a.lower() for a in added)

    # 候选：(要匹配的写法, 该术语的全部对侧写法)
    candidates: list[tuple[str, list[str]]] = []
    for term in GLOSSARY:
        en_forms = [f for f in (term.en, *term.alt) if f.isascii()]
        zh_forms = [term.zh, *[a for a in term.alt if not a.isascii()]]
        for zh in zh_forms:
            candidates.append((zh, en_forms))
        for en in en_forms:
            candidates.append((en, zh_forms))

    # 长的先匹配
    candidates.sort(key=lambda c: len(c[0]), reverse=True)

    for needle, replacements in candidates:
        if len(added) >= max_added:
            break
        if not needle:
            continue
        is_ascii = needle.isascii()
        key = needle.lower() if is_ascii else needle
        hay = q_lower if is_ascii else query
        for span in _match_spans(hay, key):
            if not _free(span, covered):
                continue
            covered.append(span)
            for r in replacements:
                if r and not _already_present(r):
                    added.append(r)
            break

    return added


def expand_query(query: str, max_added: int = 16) -> str:
    """把查询里命中的术语，附上它对侧语言的写法。

    ## 为什么要处理"长词优先"

    「营业利润」里含「利润」。如果两个都扩展，
    查询会同时带上 `operating income` 和**裸的** `income`，
    后者是个噪音词（会命中一堆无关片段）。

    做法：**按词长从长到短匹配，已经被长词覆盖的位置不再匹配短词。**
    这样「营业利润」占住位置后，「利润」就不会再被单独匹配。

    ## 两条实测修出来的规则

    **① 一个术语命中，它的所有写法都要加。**
       「总资产」除了 `total assets` 还该带上 `Assets` ——
       后者是官方 XBRL 标签，索引里的表格标题带它。
       原来只加主写法，`Assets` 永远够不着。

    **② 已经出现在查询里的词不再追加。**
       否则 `expand(expand(q))` 会把每个词加两遍（重复词会人为抬高 BM25 分数）。

    返回：原查询 + 扩展词（用空格连起来）。没命中任何术语时原样返回。
    """
    added = expand_terms(query, max_added)
    if not added:
        return query
    return query + " " + " ".join(added)


def detect_language(text: str) -> str:
    """粗判语言：中文占比超过 20% 就算中文。用于给出提示，不用于逻辑分支。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    letters = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0:
        return "en"
    if letters == 0:
        return "zh"
    return "zh" if cjk / (cjk + letters / 3) > 0.2 else "en"
