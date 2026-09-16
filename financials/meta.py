"""推断口径 —— 会计准则、合并 / 单体。

## 为什么需要（实测）

两个装载器都把口径**写死**了：

    from_excel.load_excel_statements   gaap="CAS",  scope="单体"
    from_pdf.load_pdf_statements       gaap="CAS",  scope="合并"

于是实测到两处明显报错的口径：

    苏州井利电子 2024 审计报告    非上市的**单体**审计报告 → 被报成「合并」
    Cicero（美国）报表            美国公司 → 被报成「CAS 中国会计准则」

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
    # 利润表 —— 第一版只放资产负债表那些，导致 Cicero 的利润表判成「未判定」
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
#: 结果**茅台**被判成 1993 年格式 —— 因为现代资产负债表的
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
