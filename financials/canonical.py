"""三张表 → 标准科目。

## 这一块为什么难

估值引擎要的不是"一张表"，是**几个特定口径的数字**：
净债务、少数股东权益、折旧摊销、资本开支……

中间隔着一层映射。而映射难在：**会计科目不是确定清单** ——
公司会自创科目名（「交易性金融资产」和「以公允价值计量且其变动
计入当期损益的金融资产」是同一个东西）。

## 两条可用的锚

**① 美股：官方 XBRL 标签是确定答案。**
   `us-gaap:CashAndCashEquivalentsAtCarryingValue` 就是现金。
   SEC 申报文件的 HTML 里内嵌了标签（见 `ingest/html.py`），
   所以这条路上映射是**查表**。

**② 中国材料：按科目名映射。**
   企业会计准则的科目名是规范的、有公开标准的，可以建表。
   自创名靠别名覆盖。

## 勾稽校验是这一层的验收标准

映射对不对，不用人眼看 —— 三条勾稽关系能客观判定（§4.5）：

    资产 = 负债 + 所有者权益
    期末现金 = 期初 + 经营 + 投资 + 筹资
    净利润 → 经营现金流的间接法调节

**平了，说明映射对了。** 平不了，说明某一行的归属错了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Field(str, Enum):
    """标准科目。值是给人看的中文名（报错信息里会用到）。"""

    # ---- 资产负债表：时点值 ----
    CASH = "货币资金"
    SHORT_TERM_INVESTMENTS = "交易性金融资产"
    ACCOUNTS_RECEIVABLE = "应收账款"
    INVENTORY = "存货"
    OTHER_CURRENT_ASSETS = "其他流动资产"
    TOTAL_CURRENT_ASSETS = "流动资产合计"
    PPE = "固定资产"
    GOODWILL = "商誉"
    INTANGIBLES = "无形资产"
    OTHER_NONCURRENT_ASSETS = "其他非流动资产"
    TOTAL_ASSETS = "资产总计"

    ACCOUNTS_PAYABLE = "应付账款"
    DEFERRED_REVENUE = "预收款项"
    SHORT_TERM_DEBT = "短期借款"
    ACCRUED_LIABILITIES = "其他应付款"
    TAXES_PAYABLE = "应交税费"
    OTHER_CURRENT_LIABILITIES = "其他流动负债"
    TOTAL_CURRENT_LIABILITIES = "流动负债合计"
    LONG_TERM_DEBT = "长期借款"
    OTHER_NONCURRENT_LIABILITIES = "其他非流动负债"
    TOTAL_LIABILITIES = "负债合计"
    MINORITY_INTEREST = "少数股东权益"
    EQUITY = "所有者权益合计"
    TOTAL_EQUITY_AND_LIABILITIES = "负债和所有者权益总计"

    # ---- 利润表：期间值 ----
    REVENUE = "营业收入"
    COST_OF_REVENUE = "营业成本"
    GROSS_PROFIT = "毛利"
    OPERATING_EXPENSES = "营业费用合计"
    RND = "研发费用"
    SGNA = "销售及管理费用"
    OPERATING_INCOME = "营业利润"
    DEPRECIATION_AMORTIZATION = "折旧与摊销"
    INTEREST_EXPENSE = "利息费用"
    INTEREST_INCOME = "利息收入"
    PRETAX_INCOME = "利润总额"
    INCOME_TAX = "所得税费用"
    NET_INCOME = "净利润"

    # ---- 现金流量表：期间值 ----
    CFO = "经营活动产生的现金流量净额"
    CFI = "投资活动产生的现金流量净额"
    CFF = "筹资活动产生的现金流量净额"
    CAPEX = "购建固定资产等支付的现金"
    FX_EFFECT = "汇率变动影响"
    NET_CASH_CHANGE = "现金及现金等价物净增加额"
    CASH_BEGIN = "期初现金及现金等价物余额"
    CASH_END = "期末现金及现金等价物余额"
    # 间接法调节项
    ID_DA = "间接法：折旧摊销"
    ID_STOCK_COMP = "间接法：股份支付"
    ID_WORKING_CAPITAL = "间接法：营运资本变动"

    # ---- 结构项（不是金额，是表的骨架） ----
    SECTION = "分节标题"


#: 表结构类标签，不是金额 —— 映射到 SECTION
_ABSTRACT_SUFFIX = "Abstract"


@dataclass(frozen=True)
class Mapping:
    """一个标准科目 ↔ 它的各种写法。"""

    field: Field
    #: 中文科目名（企业会计准则的规范名 + 常见别名）
    names: tuple[str, ...] = ()
    #: XBRL 标签（不含 `us-gaap:` 前缀）
    tags: tuple[str, ...] = ()


#: 映射表。
#:
#: **顺序有意义**：先匹配更具体的科目。比如「流动资产合计」必须
#: 排在「流动资产」前面，否则前者会被后者吃掉。
MAPPINGS: tuple[Mapping, ...] = (
    # ================= 资产负债表 =================
    Mapping(Field.TOTAL_ASSETS, ("资产总计", "资产合计", "总资产"),
            ("Assets",)),
    Mapping(Field.TOTAL_CURRENT_ASSETS, ("流动资产合计",),
            ("AssetsCurrent",)),
    Mapping(Field.CASH, ("货币资金", "现金及现金等价物", "现金及银行存款"),
            ("CashAndCashEquivalentsAtCarryingValue",)),
    Mapping(Field.SHORT_TERM_INVESTMENTS,
            ("交易性金融资产", "短期投资", "以公允价值计量且其变动计入当期损益的金融资产"),
            ("AvailableForSaleSecuritiesDebtSecuritiesCurrent",
             "MarketableSecuritiesCurrent", "ShortTermInvestments")),
    Mapping(Field.ACCOUNTS_RECEIVABLE, ("应收账款", "应收款项", "应收账款净额"),
            ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent")),
    Mapping(Field.INVENTORY, ("存货", "存货净额"), ("InventoryNet", "InventoryFinishedGoods")),
    Mapping(Field.PPE, ("固定资产", "固定资产净额", "物业厂房及设备"),
            ("PropertyPlantAndEquipmentNet",)),
    Mapping(Field.GOODWILL, ("商誉",), ("Goodwill",)),
    Mapping(Field.INTANGIBLES, ("无形资产", "无形资产净额"),
            ("IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet")),
    Mapping(Field.OTHER_CURRENT_ASSETS,
            ("预付款项", "预付账款", "其他流动资产", "一年内到期的非流动资产"),
            ("PrepaidExpenseAndOtherAssetsCurrent", "OtherAssetsCurrent",
             "PrepaidExpenseCurrent")),
    Mapping(Field.OTHER_NONCURRENT_ASSETS,
            ("其他非流动资产", "长期股权投资", "递延所得税资产", "其他资产"),
            ("OtherAssetsNoncurrent", "DeferredTaxAssetsLiabilitiesNetNoncurrent",
             "DeferredTaxAssetsNetNoncurrent")),
    Mapping(Field.ACCRUED_LIABILITIES, ("其他应付款", "应计费用", "预提费用"),
            ("AccruedLiabilitiesCurrent", "AccruedLiabilities",
             "EmployeeRelatedLiabilitiesCurrent")),
    Mapping(Field.TAXES_PAYABLE, ("应交税费", "应付税费", "应交税金"),
            ("TaxesPayableCurrent", "IncomeTaxesPayable",
             "AccruedIncomeTaxesCurrent")),
    Mapping(Field.OTHER_CURRENT_LIABILITIES,
            ("其他流动负债", "一年内到期的非流动负债"),
            ("OtherLiabilitiesCurrent",)),
    Mapping(Field.OTHER_NONCURRENT_LIABILITIES,
            ("其他非流动负债", "递延所得税负债", "长期应付款", "其他负债"),
            ("OtherLiabilitiesNoncurrent", "DeferredTaxLiabilitiesNoncurrent")),

    Mapping(Field.TOTAL_LIABILITIES, ("负债合计", "负债总计", "总负债"),
            ("Liabilities",)),
    Mapping(Field.TOTAL_CURRENT_LIABILITIES, ("流动负债合计",),
            ("LiabilitiesCurrent",)),
    Mapping(Field.ACCOUNTS_PAYABLE, ("应付账款", "应付款项"),
            ("AccountsPayableCurrent",)),
    Mapping(Field.DEFERRED_REVENUE, ("预收款项", "预收账款", "递延收入"),
            ("DeferredRevenueCurrent", "ContractWithCustomerLiabilityCurrent")),
    Mapping(Field.SHORT_TERM_DEBT, ("短期借款", "短期负债"),
            ("ShortTermBorrowings", "ShortTermDebt", "LongTermDebtCurrent")),
    Mapping(Field.LONG_TERM_DEBT, ("长期借款", "长期负债"),
            ("LongTermDebtNoncurrent", "LongTermDebt")),
    Mapping(Field.MINORITY_INTEREST, ("少数股东权益",),
            ("MinorityInterest", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
    Mapping(Field.EQUITY, ("所有者权益合计", "股东权益合计", "所有者权益", "净资产"),
            ("StockholdersEquity",)),
    Mapping(Field.TOTAL_EQUITY_AND_LIABILITIES,
            ("负债和所有者权益总计", "负债及所有者权益总计", "负债与所有者权益总计"),
            ("LiabilitiesAndStockholdersEquity",)),

    # ================= 利润表 =================
    Mapping(Field.REVENUE, ("营业收入", "营业总收入", "主营业务收入", "销售收入"),
            ("RevenueFromContractWithCustomerExcludingAssessedTax",
             "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax",
             "SalesRevenueNet", "SalesRevenueGoodsNet")),
    Mapping(Field.COST_OF_REVENUE, ("营业成本", "主营业务成本", "销售成本"),
            ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")),
    Mapping(Field.GROSS_PROFIT, ("毛利", "毛利润"), ("GrossProfit",)),
    Mapping(Field.OPERATING_EXPENSES, ("营业费用合计", "营业总成本", "营业费用"),
            ("OperatingExpenses", "CostsAndExpenses")),
    Mapping(Field.RND, ("研发费用", "研发支出"),
            ("ResearchAndDevelopmentExpense",)),
    Mapping(Field.SGNA, ("销售费用", "管理费用", "销售及管理费用", "销售和管理费用"),
            ("SellingGeneralAndAdministrativeExpense",)),
    Mapping(Field.DEPRECIATION_AMORTIZATION, ("折旧与摊销", "折旧和摊销"),
            ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
             "DepreciationAmortizationAndAccretionNet")),
    Mapping(Field.OPERATING_INCOME, ("营业利润", "营业利润（亏损）"),
            ("OperatingIncomeLoss",)),
    Mapping(Field.INTEREST_EXPENSE, ("利息费用", "财务费用"),
            ("InterestExpense", "InterestExpenseNonoperating")),
    Mapping(Field.INTEREST_INCOME, ("利息收入",), ("InvestmentIncomeInterest",)),
    Mapping(Field.PRETAX_INCOME,
            ("利润总额", "税前利润", "所得税前利润"),
            ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
             "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments")),
    Mapping(Field.INCOME_TAX, ("所得税费用", "所得税"),
            ("IncomeTaxExpenseBenefit",)),
    Mapping(Field.NET_INCOME, ("净利润", "净利润（亏损）", "归属于母公司股东的净利润"),
            ("NetIncomeLoss", "ProfitLoss")),

    # ================= 现金流量表 =================
    Mapping(Field.CFO, ("经营活动产生的现金流量净额", "经营活动现金流量净额",
                        "经营活动产生的现金流量"),
            ("NetCashProvidedByUsedInOperatingActivities",)),
    Mapping(Field.CFI, ("投资活动产生的现金流量净额", "投资活动现金流量净额",
                        "投资活动产生的现金流量"),
            ("NetCashProvidedByUsedInInvestingActivities",)),
    Mapping(Field.CFF, ("筹资活动产生的现金流量净额", "筹资活动现金流量净额",
                        "筹资活动产生的现金流量"),
            ("NetCashProvidedByUsedInFinancingActivities",)),
    Mapping(Field.CAPEX, ("购建固定资产、无形资产和其他长期资产支付的现金",
                          "购建固定资产等支付的现金", "资本开支"),
            ("PaymentsToAcquirePropertyPlantAndEquipment",)),
    Mapping(Field.NET_CASH_CHANGE, ("现金及现金等价物净增加额",),
            ("CashAndCashEquivalentsPeriodIncreaseDecrease",
             "CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect")),
    Mapping(Field.FX_EFFECT,
            ("汇率变动对现金及现金等价物的影响", "汇率变动影响"),
            ("EffectOfExchangeRateOnCashAndCashEquivalents",)),
    Mapping(Field.CASH_BEGIN, ("期初现金及现金等价物余额",),
            ("CashAndCashEquivalentsAtCarryingValue",)),
    Mapping(Field.CASH_END, ("期末现金及现金等价物余额",),
            ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
             "CashAndCashEquivalentsAtCarryingValue")),
    # 间接法调节项（现金流量表的"净利润 → 经营现金流"那段）
    Mapping(Field.ID_DA, ("折旧", "摊销", "折旧及摊销", "无形资产摊销"),
            ("Depreciation", "AmortizationOfIntangibleAssets",
             "DepreciationDepletionAndAmortization",
             "AcceleratedDepreciation")),
    Mapping(Field.ID_STOCK_COMP, ("股份支付", "以权益结算的股份支付"),
            ("ShareBasedCompensation", "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost")),
)

#: 这些标签在资产负债表和现金流量表里含义不同，要按行名区分。
#:
#: `us-gaap:CashAndCashEquivalentsAtCarryingValue` 在资产负债表里是**期末余额**，
#: 在现金流量表里既可能是期末、也可能是**期初** —— 同一个标签，两个意思。
#:
#: ## 这里踩过一个坑：子串判断不能双向
#:
#: 原来写的是「模式包含行名，或行名包含模式」。结果资产负债表的
#: 「Cash and cash equivalents」（短）**被判定包含在**现金流量表的
#: 「Cash and cash equivalents at **beginning** of period」（长）里，
#: 于是期末余额被当成期初余额 —— 货币资金直接取不到。
#:
#: 改成**必须出现区分词**才认。
#:
#: ⚠️ 关键词要写成**去空格**的形式 —— `_norm` 会把空格也去掉，
#: 写成 `"end of period"` 永远匹配不上（实际文本是 `endofperiod`）。踩过。
_DISAMBIGUATING = (
    (("期初", "beginning"), Field.CASH_BEGIN),
    (("期末", "endofperiod", "atend"), Field.CASH_END),
)


def _norm(s: str) -> str:
    """比对用的归一化：去空白、去全半角括号、去标点。"""
    s = s.strip().lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[（()）【】\[\]：:，,、。.\-—_*]", "", s)
    return s


# 预先建索引，避免每次线性扫描
_BY_TAG: dict[str, Field] = {}
_BY_NAME: dict[str, Field] = {}
for _m in MAPPINGS:
    for _t in _m.tags:
        _BY_TAG.setdefault(_norm(_t), _m.field)
    for _n in _m.names:
        _BY_NAME.setdefault(_norm(_n), _m.field)


@dataclass
class MappedRow:
    """一行映射结果。"""

    label: str
    field: Field | None
    value: float | None
    via: str = ""       # tag / name / none —— 怎么匹配上的，便于排查
    tag: str | None = None


def identify(label: str, xbrl_tag: str | None = None) -> tuple[Field | None, str]:
    """判断一行是什么科目。返回 (标准科目, 依据)。

    **优先用 XBRL 标签**：官方标签是确定答案，中文科目名有别名问题。
    标签匹配不上再退回按名字匹配。

    例外：少数标签在不同表里含义不同（见 `_AMBIGUOUS_BY_LABEL`），
    这种情况下**行名比标签准**，所以先看行名。
    """
    lab = _norm(label)
    if any(k in lab for k in ("现金", "cash")):
        for keys, f in _DISAMBIGUATING:
            if any(k in lab for k in keys):
                return f, "label-disambiguated"

    if xbrl_tag:
        raw = xbrl_tag.split(":")[-1]
        if raw.endswith(_ABSTRACT_SUFFIX):
            return Field.SECTION, "tag"
        hit = _BY_TAG.get(_norm(raw))
        if hit is not None:
            return hit, "tag"

    if label.strip():
        hit = _BY_NAME.get(lab)
        if hit is not None:
            return hit, "name"

    return None, "none"
