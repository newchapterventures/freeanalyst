"""推断口径 —— 会计准则、合并 / 单体。

## 为什么需要（实测）

两个装载器都把口径**写死**了：

    from_excel.load_excel_statements   gaap="CAS",  scope="单体"
    from_pdf.load_pdf_statements       gaap="CAS",  scope="合并"

于是实测到两处明显报错的口径：

    某非上市公司 2024 审计报告    非上市的**单体**审计报告 → 被报成「合并」
    某美国公司（美国）报表            美国公司 → 被报成「CAS 中国会计准则」

**口径报错比数字报错更危险。** 数字错了勾稽会不平，有信号；口径错了
无声无息，看报告的人会按错的口径去理解手上的数。

## 原则

**推不出就说不知道。** 宁可报「未判定」，也不要给一个看起来合理的错标签。
`Statements.gaap` / `.scope` 是空字符串时下游会照原样报出来。
"""

from __future__ import annotations

import re

#: 美国准则（US GAAP）的特征科目名。**去空格、小写**后比对。
_US_MARKERS = (
    # 资产负债表
    "stockholdersequity", "shareholdersequity", "memberscontribution",
    "memberscapital", "ptopayable", "flexdeductionspayable",
    "accumulateddeficit", "retainagereceivable", "workinprogress",
    "commonstock", "additionalpaidincapital", "allowancefordoubtfulaccounts",
    "balancesheet", "pettycash", "clientfeesreceivables", "autoloans",
    # 利润表 —— 第一版只放资产负债表那些，导致 某美国公司 的利润表判成「未判定」
    "incomestatement", "costofsales", "grossprofit", "operatingincome",
    "netincome", "netloss", "incometaxexpense", "sellinggeneralandadministrative",
    "selling,generalandadministrative", "incomebeforetaxes",
    "costofgoodssold", "professionalfees",
)

#: 国际准则（IFRS）的特征写法 —— 连字符、英式拼法
_IFRS_MARKERS = (
    "non-current", "noncurrentassets", "statementoffinancialposition",
    "statementofprofitorloss", "property,plantandequipment",
    "profitfortheyear", "shareholders'equity", "financecosts",
    "tradeandotherreceivables", "cashandcashequivalents",
)

#: 中国准则的特征科目名
_CAS_MARKERS = (
    "资产负债表", "利润表", "现金流量表", "所有者权益合计", "货币资金",
    "应收账款", "应付账款", "未分配利润", "实收资本", "营业收入", "净利润",
)

#: 中国小企业会计准则 —— 财会〔2011〕17号
_CAS_SMALL = ("小企业会计准则", "会小企", "小企01表", "小企02表")

#: 1993 年「行业会计制度」的报表格式（2006 年已废止，但老企业的表还在用）。
#:
#: ⚠️ **这里只放不会误伤的标志。** 第一版放了 `待摊费用` / `预提费用`，
#: 结果**某白酒公司**被判成 1993 年格式 —— 因为现代资产负债表的
#: 「**长期**待摊费用」里就含「待摊费用」，是子串假匹配。
#:
#: `会工01表` / `会工02表` 是那套制度**特有**的表号，只在那个年代的
#: 表头上出现，没有子串风险。
_CAS_LEGACY = ("会工01表", "会工02表", "产品销售利润", "会小企01表")


def _flat(s: str) -> str:
    return re.sub(r"[\s\u3000]", "", s).lower()


def detect_gaap(text: str) -> str:
    """判断这份材料用的是什么会计准则。

    返回 `"CAS"` / `"CAS（小企业会计准则）"` / `"US GAAP"` / `"IFRS"` /
    `"未判定"`。

    **中国准则优先判** —— 中英双语的 H 股年报里两类特征词都有，
    但它首先是一份中国公司的报表，判成 IFRS 会更误导。
    """
    t = _flat(text)
    if not t:
        return "未判定"

    # **先看 1993 年的表号。** `会工01表` / `会工02表` 是那套制度特有的，
    # 出现即定性 —— 不能等「中文特征 ≥3」那条过了再判，因为老表里
    # 现代科目名本来就少（实测：只给表号 + 几个旧科目名时，计数够不上，
    # 会被漏判成「未判定」）。
    if any(_flat(m) in t for m in _CAS_LEGACY):
        return "CAS（1993 年行业会计制度格式）"

    # 中文特征 —— 只要出现足够多的中文科目名，就是中国准则
    cas_hits = sum(1 for m in _CAS_MARKERS if m in t)
    if cas_hits >= 3:
        if any(m in t for m in _CAS_SMALL):
            return "CAS（小企业会计准则）"
        return "CAS"

    us = sum(1 for m in _US_MARKERS if m in t)
    ifrs = sum(1 for m in _IFRS_MARKERS if m in t)
    if us >= 2 and us > ifrs:
        return "US GAAP"
    if ifrs >= 2 and ifrs > us:
        return "IFRS"
    if us or ifrs:
        # 只命中一两个 —— 证据不足，不猜
        return "未判定（英文材料，US GAAP / IFRS 特征都不足）"
    return "未判定"


#: 「合并」的标志
_CONSOLIDATED = ("合并资产负债表", "合并利润表", "合并现金流量表",
                 "consolidated", "合并及公司", "groupstatement")
#: 「母公司报表 / 单体」的标志
_PARENT_ONLY = ("母公司资产负债表", "母公司利润表", "母公司现金流量表",
                "parentcompany", "companylevel", "单体")


def detect_scope(text: str) -> str:
    """判断报的是合并报表还是单体报表。

    ## 判不出来的情况要老实地报出来

    非上市公司的审计报告**只有单体**（没有子公司就没有合并），
    里面根本不会出现「合并」或「母公司」这些字 —— 那就返回「单体」，
    因为这是**唯一可能**，不是猜。

    上市公司年报里合并和母公司两套都有，这时必须看标题说的是哪个。
    """
    t = _flat(text)
    if not t:
        return "未判定"
    if any(_flat(m) in t for m in _CONSOLIDATED):
        return "合并"
    if any(_flat(m) in t for m in _PARENT_ONLY):
        return "母公司报表"
    return "单体"


#: 金额单位。中文是「单位：万元」/「人民币千元」，英文是「(In thousands)」。
#:
#: ## 为什么单位要单独判（实测踩到）
#:
#: 报表是**千美元**、引擎默认**万元** —— 认错是 1000 倍级的**静默**错误：
#: 数看着正常，只是全部错了三个数量级。所以这层的纪律是
#: **认不出就返回空**，让调用方停下来问人，不许猜。
_UNIT_CN = re.compile(
    r"单位\s*(?:为|[:：])?\s*(?:人民币|RMB)?\s*"
    r"(亿元|万元|千元|百万元|元"
    r"|千美元|百万美元|万美元|美元)")
_UNIT_WORD = (
    ("人民币千元", "千元"), ("人民币百万元", "百万元"), ("人民币万元", "万元"),
    ("人民币元", "元"), ("千美元", "千美元"), ("百万美元", "百万美元"),
    ("万美元", "万美元"), ("美元", "美元"), ("万元", "万元"), ("千元", "千元"),
)
_UNIT_EN = (
    (r"in\s+thousands", "千美元"), (r"in\s+millions", "百万美元"),
)
#: **带货币的紧凑写法**：`RMB’000` / `RMB'000` / `US$’000` / `HK$’000` ——
#: 港交所与 IFRS 报表页首就是这样标的。
#:
#: ## 必须排在 `_UNIT_EN` 前面（实测踩到）
#: 某港股年报正文里另有一处 "in millions"（附注里另一段的口径），
#: 而它的正表标的是 `RMB’000` → 整份材料的单位被判成**「百万美元」** ✗✗：
#: **货币和量级两个都错**（实际是人民币千元）。单位错是 1000 倍级的静默错误，
#: 所以判据要挑**最具体、最靠近正表**的那个，而不是先撞上谁算谁。
_UNIT_HK = (
    (re.compile(r"RMB\s*['’]?\s*0{3}\b", re.I), "千元"),
    (re.compile(r"RMB\s*['’]?\s*0{6}\b", re.I), "百万元"),
    (re.compile(r"(?:US\$|USD|美元)\s*['’]?\s*0{3}\b", re.I), "千美元"),
    (re.compile(r"(?:HK\$|HKD|港币|港元)\s*['’]?\s*0{3}\b", re.I), "千港元"),
    (re.compile(r"RMB\s+(?:in\s+)?million", re.I), "百万元"),
)


#: 金融企业报表的标志（**暂未启用**，见下面的说明）。
#:
#: ## ⚠️ 已知未解：金融业判定试过一版，**失败了，已退回**
#:
#: 目的：金融企业（保险/银行/券商）的「EBITDA」「资本开支」「净营运资本」不适用，
#: 界面该说「不适用」而不是「无值」（两者的处理方式完全相反）。
#:
#: 试过两版判据，都不成立：
#:   1. 喂**整份正文** → 某 377 页矿业公司审计报告的关联方/或有事项章节里
#:      出现「吸收存款」「客户贷款」→ 矿业公司被判成金融 ✗
#:   2. 喂**三张表的行标签**（更严）→ **仍然误判**：实测那份材料的三张表行里
#:      真有「吸收存款」「存放中央银行」「拆出资金」「买入返售金融资产」
#:      乃至「赔付支出」「保险责任准备金」「已赚保费」—— 集团旗下有金融/保险业务，
#:      这些科目**确实出现在报表行上** ✗
#:
#: **误判的代价大于不判**：它会把一份完全正常的工商业材料标成"口径不适用"，
#: 把四个参考值全关掉 —— 比原来的"显示数值"更坏。
#:
#: 要做对，需要更严的判据（例如"金融科目占资产/收入的主体"这类**占比**判据），
#: 而那必须先拿**多份真材料**量出阈值 —— 在量出来之前，这一层保持关闭。
_FIN_SIGNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("保险", ("保险服务收入", "保險服務收入", "赔付支出", "賠付支出",
              "保险合同负债", "保險合同負債", "保险责任准备金", "保險責任準備金",
              "承保财务损益", "分出保费", "退保金", "已赚保费")),
    ("银行", ("客户贷款及垫款", "客戶貸款及墊款", "吸收存款", "存放中央银行",
              "利息净收入", "利息淨收入", "拆出资金", "买入返售金融资产",
              "卖出回购金融资产款", "贷款减值准备")),
    ("券商", ("手续费及佣金净收入", "手續費及佣金淨收入", "融出资金",
              "代理买卖证券款", "自营业务收入", "客户保证金")),
)


def detect_unit(text: str) -> tuple[str, str]:
    """从材料正文里认金额单位。返回 `(单位, 依据)`；**认不出返回空**。"""
    if not text:
        return "", ""
    m = _UNIT_CN.search(text)
    if m:
        return m.group(1), f"正文里的「{m.group(0).strip()}」"
    head = text[:200_000]
    # **带货币的紧凑写法优先**：`RMB’000` 比笼统的 "in millions" 具体得多，
    # 而且通常就在正表页首（见 `_UNIT_HK` 的说明）。
    for pat, unit in _UNIT_HK:
        m2 = pat.search(head)
        if m2:
            return unit, f"正文里的「{m2.group(0).strip()}」"
    low = head.lower()
    # **语言自洽的写法优先于笼统的英文短语。** 「人民币千元」「千元」出现在中文材料里，
    # 比正文某处一句 "in millions" 具体得多；实测正是后者把港股那份判成了「百万美元」。
    for word, unit in _UNIT_WORD:
        if word in head:
            return unit, f"正文里的「{word}」"
    for pat, unit in _UNIT_EN:
        if re.search(pat, low):
            # ★ 这里**必须说实话**：`in thousands` 里**没有币种**。
            #   把它当成美元是一个假设，不是认出来的 —— 而这一层的纪律本来就是
            #   "认不出就返回空，不许猜"。暂时保留映射（换掉会改动既有行为），
            #   但依据里写明是**假设**，让报告里读得到。
            return unit, (f"英文材料里的「{pat}」——⚠️ **这段只写量级、没写币种**，"
                          f"按「{unit}」处理是**假设**，请核对是不是人民币/港元口径")
    for word, unit in _UNIT_WORD:
        if word in head:
            return unit, f"正文里的「{word}」"
    return "", ""


#: 期间：中文「2024年12月31日」，英文两种写法 ——
#: **「December 31, 2020」和「31 December 2020」都要认。**
#: 港股/英式报表用后者，第一版只写前者，于是某 H 股年报的期间一直判不出来。
_PERIOD_CN = re.compile(r"(19|20)\d{2}\s*[-/年]\s*\d{1,2}(?:\s*[-/月]\s*\d{1,2}\s*日?)?")
_PERIOD_MONTH = (r"(?:January|February|March|April|May|June|July|August|September"
                 r"|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep"
                 r"|Sept|Oct|Nov|Dec)")
_PERIOD_EN = re.compile(
    rf"(?:{_PERIOD_MONTH}\.?\s+\d{{1,2}},\s*(?:19|20)\d{{2}}"
    rf"|\d{{1,2}}\s+{_PERIOD_MONTH}\.?,?\s+(?:19|20)\d{{2}})")

#: **中文数字年份**：审计报告和传统排版写「二〇二四年度」「二零二四年十二月三十一日」。
#: 实测一份审计报告的期间**只有这种写法** → 期间判不出来 → 估值基准日退化成「1…N」占位，
#: 报告里多一条本可避免的缺口 ✗。
_CN_YEAR = re.compile(r"([〇零一二三四五六七八九]{2,4})\s*年")
#: 只认年报那套小写中文数字。**不认「壹贰叁」大写** —— 那是金额的写法，不是年份。
_CN_DIGIT = {"〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _cn_year(s: str) -> str:
    """「二〇二四」→「2024」。凑不成 19xx/20xx 就返回空（**不猜**）。"""
    d = "".join(_CN_DIGIT.get(c, "") for c in s)
    return d if len(d) == 4 and d[:2] in ("19", "20") else ""


def detect_period(text: str) -> str:
    """从材料正文里认报表期间（资产负债表日）。**认不出返回空。**"""
    if not text:
        return ""
    m = _PERIOD_CN.search(text) or _PERIOD_EN.search(text)
    if m:
        return m.group(0)
    m2 = _CN_YEAR.search(text)
    if m2:
        y = _cn_year(m2.group(1))
        if y:
            # **只说材料写了什么**：原表只写年度，就回年度 ——
            # 编一个「12月31日」比不给更坏（价格、期间对齐都会跟着错）。
            return f"{y}年（原表写作「{m2.group(0).strip()}」）"
    return ""
