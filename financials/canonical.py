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
    TOTAL_NONCURRENT_ASSETS = "非流动资产合计"
    PPE = "固定资产"
    GOODWILL = "商誉"
    INTANGIBLES = "无形资产"
    OTHER_NONCURRENT_ASSETS = "其他非流动资产"
    TOTAL_ASSETS = "资产总计"

    ACCOUNTS_PAYABLE = "应付账款"
    DEFERRED_REVENUE = "预收款项"
    SHORT_TERM_DEBT = "短期借款"
    ACCRUED_LIABILITIES = "其他应付款"
    EMPLOYEE_PAYABLE = "应付职工薪酬"
    TAXES_PAYABLE = "应交税费"
    OTHER_CURRENT_LIABILITIES = "其他流动负债"
    TOTAL_CURRENT_LIABILITIES = "流动负债合计"
    TOTAL_NONCURRENT_LIABILITIES = "非流动负债合计"
    LONG_TERM_DEBT = "长期借款"
    OTHER_NONCURRENT_LIABILITIES = "其他非流动负债"
    TOTAL_LIABILITIES = "负债合计"
    MINORITY_INTEREST = "少数股东权益"
    EQUITY_PARENT = "归属于母公司所有者权益"
    EQUITY = "所有者权益合计"
    # ---- IFRS 的「净资产列报式」（H 股常见）----
    #:
    #: 这种格式**根本没有「资产总计」和「负债合计」行**，而是列：
    #:
    #:     非流动资产 … 小计
    #:     流动资产   … 小计
    #:     流动负债   … 小计
    #:     流动资产净额            = 流动资产 − 流动负债
    #:     总资产减流动负债         = 非流动资产 + 流动资产净额
    #:     非流动负债 … 小计
    #:     资产净额               = 总资产减流动负债 − 非流动负债
    #:     权益总额               = 资产净额
    #:
    #: 所以 `资产 = 负债 + 权益` 那条检查**用不了**（缺两个总计数），
    #: 但 `资产净额 = 权益总额` 是等价的校验，必须单独做。
    #: `资产净额` 和 `权益总额` 要分开映射，否则两者会互相覆盖，
    #: 校验也就没了。
    NET_CURRENT_ASSETS = "流动资产净额"
    ASSETS_LESS_CURRENT_LIABILITIES = "总资产减流动负债"
    NET_ASSETS = "资产净额"
    TOTAL_EQUITY_AND_LIABILITIES = "负债和所有者权益总计"
    # ---- 权益明细（勾稽用不到，但要能认出，否则会被当成「未映射」噪音）----
    CAPITAL_STOCK = "实收资本"
    CAPITAL_RESERVE = "资本公积"
    TREASURY_STOCK = "库存股"
    OCI = "其他综合收益"
    SURPLUS_RESERVE = "盈余公积"
    GENERAL_RISK_RESERVE = "一般风险准备"
    RETAINED_EARNINGS = "未分配利润"

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

    # ---- 1993 年「行业会计制度」的科目（小企业/WPS 老模板实测）----
    #:
    #: 中小企业的报表常是老会计制度格式，科目名和现行准则**不是一套词**：
    #:
    #:     旧：产品销售收入 / 产品销售成本 / 产品销售税金及附加
    #:     新：营业收入   / 营业成本   / 税金及附加
    #:
    #: 实测某小企业 2024 年报表：损益表 17 个科目只认出 6 个（35%）。
    #: 这些名字要显式认识，否则整张表都是「未映射」噪音，
    #: 而且**勾稽需要的行取不到**。
    NOTES_RECEIVABLE = "应收票据"
    NOTES_PAYABLE = "应付票据"
    PPE_GROSS = "固定资产原价"
    ACCUM_DEPRECIATION = "累计折旧"
    PREPAID_EXPENSES = "待摊费用"
    DIVIDENDS_PAYABLE = "应付利润"
    SELLING_EXPENSE = "销售费用"
    ADMIN_EXPENSE = "管理费用"
    TAX_SURCHARGE = "税金及附加"
    INVESTMENT_INCOME = "投资收益"
    OTHER_INCOME = "其他业务利润"
    NONOPERATING_INCOME = "营业外收入"
    NONOPERATING_EXPENSE = "营业外支出"

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
    Mapping(Field.CASH, ("货币资金", "现金及现金等价物", "现金及银行存款",
                         # H 股 / IFRS 写法
                         "现金及银行结余", "银行结余及现金",
                         "Cash and bank balances", "Cash and cash equivalents"),
            ("CashAndCashEquivalentsAtCarryingValue",)),
    Mapping(Field.SHORT_TERM_INVESTMENTS,
            ("交易性金融资产", "短期投资", "以公允价值计量且其变动计入当期损益的金融资产"),
            ("AvailableForSaleSecuritiesDebtSecuritiesCurrent",
             "MarketableSecuritiesCurrent", "ShortTermInvestments")),
    Mapping(Field.ACCOUNTS_RECEIVABLE, ("应收账款", "应收款项", "应收账款净额",
                                        # H 股 / IFRS 写法
                                        "贸易应收款项", "应收账款及票据",
                                        "Trade receivables", "Trade and other receivables"),
            ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent")),
    Mapping(Field.INVENTORY, ("存货", "存货净额"), ("InventoryNet", "InventoryFinishedGoods")),
    Mapping(Field.PPE, ("固定资产", "固定资产净额", "物业厂房及设备",
                        # 旧制度：净值 / 合计两种写法
                        "固定资产净值", "固定资产合计",
                        "物業、廠房及設備", "物業廠房及設備"),
            ("PropertyPlantAndEquipmentNet",)),
    Mapping(Field.GOODWILL, ("商誉",), ("Goodwill",)),
    Mapping(Field.INTANGIBLES, ("无形资产", "无形资产净额"),
            ("IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet")),
    Mapping(Field.OTHER_CURRENT_ASSETS,
            ("预付款项", "预付账款", "其他流动资产", "一年内到期的非流动资产",
             # 旧制度
             "应收股利", "应收利息", "一年内到期的长期债券投资"),
            ("PrepaidExpenseAndOtherAssetsCurrent", "OtherAssetsCurrent",
             "PrepaidExpenseCurrent")),
    # 金融类资产（财务公司业务；某白酒公司这类公司会有）
    Mapping(Field.OTHER_CURRENT_ASSETS,
            ("拆出资金", "买入返售金融资产", "发放贷款和垫款", "结算备付金",
             "衍生金融资产", "应收款项融资", "其他应收款"),
            ()),
    Mapping(Field.OTHER_NONCURRENT_ASSETS,
            ("其他非流动资产", "长期股权投资", "递延所得税资产", "其他资产",
             "在建工程", "使用权资产", "长期待摊费用", "投资性房地产",
             "开发支出", "生产性生物资产", "油气资产",
             # 旧制度：这些科目现行准则已废止，但小企业老模板上还在用
             "长期投资", "递延资产", "递延及无形资产合计", "其他长期资产",
             "递延税款借项", "待处理流动资产净损失", "待处理固定资产净损失",
             "固定资产清理", "无形及递延资产合计",
             "债权投资", "其他债权投资", "其他非流动金融资产", "长期应收款"),
            ("OtherAssetsNoncurrent", "DeferredTaxAssetsLiabilitiesNetNoncurrent",
             "DeferredTaxAssetsNetNoncurrent")),
    Mapping(Field.TOTAL_NONCURRENT_ASSETS, ("非流动资产合计",), ()),
    Mapping(Field.ACCRUED_LIABILITIES, ("其他应付款", "应计费用", "预提费用",
                                        # H 股 / IFRS 写法
                                        "应计及其他应付款项", "其他应付款及应计费用",
                                        "Accruals and other payables"),
            ("AccruedLiabilitiesCurrent", "AccruedLiabilities",
             "EmployeeRelatedLiabilitiesCurrent")),
    # 应付职工薪酬是新准则里的独立科目（旧准则并进「其他应付款」）。
    Mapping(Field.EMPLOYEE_PAYABLE,
            ("应付职工薪酬", "应付工资", "应付福利费"),
            ("EmployeeRelatedLiabilitiesCurrent", "AccruedPayroll")),
    Mapping(Field.TAXES_PAYABLE, ("应交税费", "应付税费", "应交税金",
                                  # 旧制度
                                  "未交税金"),
            ("TaxesPayableCurrent", "IncomeTaxesPayable",
             "AccruedIncomeTaxesCurrent")),
    Mapping(Field.OTHER_CURRENT_LIABILITIES,
            ("其他流动负债", "一年内到期的非流动负债",
             # 旧制度
             "一年内到期的长期负债", "其他未交款",
             "吸收存款及同业存放", "卖出回购金融资产款", "代理买卖证券款",
             "应付手续费及佣金", "持有待售负债", "衍生金融负债", "应付票据"),
            ("OtherLiabilitiesCurrent",)),
    Mapping(Field.OTHER_NONCURRENT_LIABILITIES,
            ("其他非流动负债", "递延所得税负债", "长期应付款", "其他负债",
             # 旧制度
             "其他长期负债", "递延税款贷项",
             "租赁负债", "长期应付职工薪酬", "预计负债", "递延收益",
             "保险合同准备金", "应付债券"),
            ("OtherLiabilitiesNoncurrent", "DeferredTaxLiabilitiesNoncurrent")),
    Mapping(Field.TOTAL_NONCURRENT_LIABILITIES,
            ("非流动负债合计",
             # 旧制度叫「长期负债合计」
             "长期负债合计"), ()),

    Mapping(Field.TOTAL_LIABILITIES, ("负债合计", "负债总计", "总负债"),
            ("Liabilities",)),
    Mapping(Field.TOTAL_CURRENT_LIABILITIES, ("流动负债合计",),
            ("LiabilitiesCurrent",)),
    Mapping(Field.ACCOUNTS_PAYABLE, ("应付账款", "应付款项",
                                     # H 股 / IFRS 写法
                                     "贸易应付款项", "Trade payables"),
            ("AccountsPayableCurrent",)),
    Mapping(Field.DEFERRED_REVENUE, ("预收款项", "预收账款", "递延收入", "合同负债"),
            ("DeferredRevenueCurrent", "ContractWithCustomerLiabilityCurrent")),
    Mapping(Field.SHORT_TERM_DEBT, ("短期借款", "短期负债"),
            ("ShortTermBorrowings", "ShortTermDebt", "LongTermDebtCurrent")),
    Mapping(Field.LONG_TERM_DEBT, ("长期借款", "长期负债"),
            ("LongTermDebtNoncurrent", "LongTermDebt")),
    Mapping(Field.MINORITY_INTEREST, ("少数股东权益", "非控制性权益",
                                      "Non-controlling interests"),
            ("MinorityInterest", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
    # 权益明细。**不能漏** —— 漏了会以「未映射」的形式出现在报告里，
    # 让人分不清是真没认出还是本来就不需要。
    Mapping(Field.EQUITY_PARENT, ("归属于母公司所有者权益", "归属于母公司股东权益",
                                  "归属于母公司股东的权益", "母公司所有者权益",
                                  # H 股 / IFRS 写法。**这些更具体的片段必须存在**，
                                  # 否则 `Total equity attributable to equity
                                  # shareholders of the Company` 这条会退到
                                  # 「权益总额」这个宽泛候选上，和真正的权益总额
                                  # 撞车 —— 实测差了一个少数股东权益（2,890）。
                                  "本公司权益股东应占", "本公司权益股东应占权益总额",
                                  "归属于母公司权益股东",
                                  "Total equity attributable to equity shareholders"
                                  " of the Company"),
            ()),
    Mapping(Field.CAPITAL_STOCK, ("实收资本", "股本"), ("CommonStockValue",)),
    Mapping(Field.CAPITAL_RESERVE, ("资本公积",), ("AdditionalPaidInCapital",)),
    Mapping(Field.TREASURY_STOCK, ("库存股",), ("TreasuryStockValue",)),
    Mapping(Field.OCI, ("其他综合收益", "其他权益工具"),
            ("AccumulatedOtherComprehensiveIncomeLossNetOfTax",)),
    Mapping(Field.SURPLUS_RESERVE, ("盈余公积",), ()),
    Mapping(Field.GENERAL_RISK_RESERVE, ("一般风险准备",), ()),
    Mapping(Field.RETAINED_EARNINGS, ("未分配利润", "留存收益", "未分配利润（未弥补亏损）"),
            ("RetainedEarningsAccumulatedDeficit",)),
    Mapping(Field.EQUITY, ("所有者权益合计", "股东权益合计", "所有者权益", "净资产",
                           # H 股 / IFRS 写法
                           "权益总额", "权益合计", "Total equity"),
            ("StockholdersEquity",)),
    # IFRS 净资产列报式的三行。**必须和 EQUITY 分开映射** ——
    # 合在一起的话 `资产净额 = 权益总额` 这条校验就没了。
    Mapping(Field.NET_ASSETS, ("资产净额", "Net assets"), ()),
    Mapping(Field.NET_CURRENT_ASSETS,
            ("流动资产净额", "净流动资产", "Net current assets"), ()),
    Mapping(Field.ASSETS_LESS_CURRENT_LIABILITIES,
            ("总资产减流动负债", "资产总额减流动负债",
             "Total assets less current liabilities"), ()),
    Mapping(Field.TOTAL_EQUITY_AND_LIABILITIES,
            ("负债和所有者权益总计", "负债及所有者权益总计", "负债与所有者权益总计"),
            ("LiabilitiesAndStockholdersEquity",)),

    # ================= 利润表 =================
    Mapping(Field.REVENUE, ("营业收入", "营业总收入", "主营业务收入", "销售收入",
                            # 旧制度
                            "产品销售收入", "商品销售收入", "营业收入合计"),
            ("RevenueFromContractWithCustomerExcludingAssessedTax",
             "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax",
             "SalesRevenueNet", "SalesRevenueGoodsNet")),
    Mapping(Field.COST_OF_REVENUE, ("营业成本", "主营业务成本", "销售成本",
                                    # 旧制度
                                    "产品销售成本", "商品销售成本"),
            ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")),
    Mapping(Field.GROSS_PROFIT, ("毛利", "毛利润",
                                 # 旧制度的「产品销售利润」= 收入 − 成本 − 税金及附加，
                                 # 比毛利的定义多减一项，但用途相同（中间的汇总行）
                                 "产品销售利润", "主营业务利润"),
            ("GrossProfit",)),
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

    # ================= 旧「行业会计制度」的科目 =================
    # 中小企业的报表常是这个格式。名字对不上就是整张表「未映射」，
    # 而且勾稽需要的行取不到（实测某小企业损益表只认出 6/17）。
    Mapping(Field.NOTES_RECEIVABLE, ("应收票据", "应收票据净额"),
            ("NotesReceivableNetCurrent", "ReceivablesNetCurrent")),
    Mapping(Field.NOTES_PAYABLE, ("应付票据",),
            ("NotesPayableCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent")),
    # **累计折旧是算 EBITDA 的关键**：它的年度增加额约等于当期折旧。
    # 小企业的表上没有现金流量表，这是唯一能拿到 D&A 的地方。
    Mapping(Field.ACCUM_DEPRECIATION, ("累计折旧", "减：累计折旧"),
            ("AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment",)),
    Mapping(Field.PPE_GROSS, ("固定资产原价", "固定资产原值"),
            ("PropertyPlantAndEquipmentGross",)),
    Mapping(Field.PREPAID_EXPENSES, ("待摊费用", "长期待摊费用"),
            ("PrepaidExpenseCurrent", "DeferredCostsCurrent")),
    Mapping(Field.DIVIDENDS_PAYABLE, ("应付利润", "应付股利", "未付利润"),
            ("DividendsPayableCurrent", "DividendsPayable")),
    Mapping(Field.SELLING_EXPENSE, ("销售费用", "营业费用",
                                    # 旧制度
                                    "产品销售费用", "商品销售费用"),
            ("SellingAndMarketingExpense", "SellingExpense")),
    Mapping(Field.ADMIN_EXPENSE, ("管理费用", "管理费用合计"),
            ("GeneralAndAdministrativeExpense", "AdministrativeExpense")),
    Mapping(Field.TAX_SURCHARGE, ("税金及附加", "营业税金及附加",
                                  # 旧制度
                                  "产品销售税金及附加", "商品销售税金及附加"),
            ("TaxesExcludingIncomeAndExciseTaxes",)),
    Mapping(Field.OTHER_INCOME, ("其他业务利润", "其他业务收入", "补贴收入"),
            ("OtherNonoperatingIncomeExpense", "OtherOperatingIncomeExpenseNet")),
    Mapping(Field.INVESTMENT_INCOME, ("投资收益", "投资损失"),
            ("InvestmentIncomeInterest", "EquityMethodInvestmentRealizedGainLossOnDisposal")),
    Mapping(Field.NONOPERATING_INCOME, ("营业外收入",),
            ("NonoperatingIncomeExpense", "OtherNonoperatingIncome")),
    Mapping(Field.NONOPERATING_EXPENSE, ("营业外支出",),
            ("OtherNonoperatingExpense", "NonoperatingExpense")),

    # ================= 现金流量表 =================
    Mapping(Field.CFO, ("经营活动产生的现金流量净额", "经营活动现金流量净额",
                        "经营活动产生的现金流量",
                        # H 股 / IFRS 写法：`所用` 而不是 `产生的`。
                        # 净流出时英文写 "net cash used in operating activities"。
                        # **实测就是这一处差异让某 H 股公司的现金勾稽判不了。**
                        "经营活动所用现金净额", "经营活动所用的现金净额",
                        "经营活动现金流出净额",
                        "Net cash used in operating activities",
                        "Net cash generated from operating activities"),
            ("NetCashProvidedByUsedInOperatingActivities",)),
    Mapping(Field.CFI, ("投资活动产生的现金流量净额", "投资活动现金流量净额",
                        "投资活动产生的现金流量",
                        "投资活动所用的现金净额", "投资活动所用现金净额",
                        "Net cash used in investing activities"),
            ("NetCashProvidedByUsedInInvestingActivities",)),
    Mapping(Field.CFF, ("筹资活动产生的现金流量净额", "筹资活动现金流量净额",
                        "筹资活动产生的现金流量", "融资活动产生的现金流量净额",
                        "融资活动所用的现金净额", "融资活动所用现金净额",
                        "Net cash used in financing activities"),
            ("NetCashProvidedByUsedInFinancingActivities",)),
    Mapping(Field.CAPEX, ("购建固定资产、无形资产和其他长期资产支付的现金",
                          "购建固定资产等支付的现金", "资本开支"),
            ("PaymentsToAcquirePropertyPlantAndEquipment",)),
    Mapping(Field.NET_CASH_CHANGE, ("现金及现金等价物净增加额",
                                    # H 股写法（减少时）
                                    "现金及现金等价物减少净额", "现金及现金等价物净减少额",
                                    "现金及现金等价物增加净额", "现金及现金等价物净增加/减少额",
                                    "Net decrease in cash and cash equivalents",
                                    "Net increase in cash and cash equivalents"),
            ("CashAndCashEquivalentsPeriodIncreaseDecrease",
             "CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect")),
    Mapping(Field.FX_EFFECT,
            ("汇率变动对现金及现金等价物的影响", "汇率变动影响",
             # H 股写法
             "汇率波动之影响", "汇率变动之影响", "外汇汇率变动的影响",
             "Effect of foreign exchange rate changes",
             "Effect of exchange rate changes on cash and cash equivalents"),
            ("EffectOfExchangeRateOnCashAndCashEquivalents",)),
    Mapping(Field.CASH_BEGIN, ("期初现金及现金等价物余额", "现金及现金等价物期初余额",
                               # H 股常用「年初 / 年末」而不是「期初 / 期末」，
                               # 而且中文在 PDF 里和英文**交错**排列：
                               #   `Cash and cash equivalents at 年初的現金及現金等價物
                               #    the beginning of the year`
                               # 拆出来的中文片段是完整的，所以按片段名匹配有效。
                               "年初现金及现金等价物", "年初的现金及现金等价物",
                               "年初现金及现金等价物余额",
                               "Cash and cash equivalents at beginning of year",
                               "Cash and cash equivalents at the beginning of the year"),
            ("CashAndCashEquivalentsAtCarryingValue",)),
    Mapping(Field.CASH_END, ("期末现金及现金等价物余额", "现金及现金等价物期末余额",
                             "年末现金及现金等价物", "年末的现金及现金等价物",
                             "年末现金及现金等价物余额", "年终现金及现金等价物",
                             "Cash and cash equivalents at end of year",
                             "Cash and cash equivalents at the end of the year"),
            ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
             "CashAndCashEquivalentsAtCarryingValue")),
    # 间接法调节项（现金流量表的"净利润 → 经营现金流"那段）
    Mapping(Field.ID_DA, ("折旧", "摊销", "折旧及摊销", "无形资产摊销"),
            ("Depreciation", "AmortizationOfIntangibleAssets",
             "DepreciationDepletionAndAmortization",
             "AcceleratedDepreciation")),
    Mapping(Field.ID_STOCK_COMP, ("股份支付", "以权益结算的股份支付"),
            ("ShareBasedCompensation", "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost")),

    # ========== 美国 / 英文材料的人读科目名 ==========
    #
    # 上面那些英文名是 **SEC EDGAR 的 XBRL 驼峰标签**（`AssetsCurrent`），
    # 只在 `datasources/sec_edgar.py` 那条路上用得上。
    #
    # 但**美国公司的 Excel / PDF 报表用的是人读的英文** ——
    # 实测 MBA 某材料 那两份（某美国公司，一家多分部的美国工程公司）：
    # 30 个科目名一个都没匹配上，而且**整个结果为空、不报错**。
    #
    # 英文名按 `_norm` 之后的**精确**匹配（`Assets` 不会吃掉 `Total Assets`），
    # 所以顺序不敏感。
    Mapping(Field.TOTAL_ASSETS, ("Total Assets",)),
    Mapping(Field.TOTAL_CURRENT_ASSETS, ("Total Current Assets",)),
    Mapping(Field.TOTAL_NONCURRENT_ASSETS, ("Total Non-Current Assets",
                                            "Total Noncurrent Assets",
                                            "Total Other Assets")),
    Mapping(Field.CASH, ("Petty Cash", "Operating Account", "Cash and Cash Equivalents",
                         "Cash and Bank Balances", "Cash at Bank and in Hand")),
    Mapping(Field.SHORT_TERM_INVESTMENTS, ("Money Market Savings", "Money Market Account",
                                           "Short-Term Investments")),
    Mapping(Field.ACCOUNTS_RECEIVABLE, ("Client Fees Receivables", "Accounts Receivable",
                                        "Trade Receivables", "Retainage Receivable",
                                        "Accounts Receivable Net")),
    Mapping(Field.PREPAID_EXPENSES, ("Prepaid Expenses", "Prepaid Insurance")),
    Mapping(Field.INVENTORY, ("Work in Progress", "Work-in-Progress", "WIP Inventory")),
    Mapping(Field.PPE, ("Property and Equipment", "Total Property and Equipment",
                        "Property, Plant and Equipment", "Furniture and Fixtures",
                        "Leasehold Improvements", "Office Equipment",
                        "Machinery and Equipment", "Net Fixed Assets",
                        "Equipment", "Automobiles", "Vehicles", "Computer Equipment")),
    Mapping(Field.INTEREST_INCOME, ("Interest Income", "Interest Revenue")),
    Mapping(Field.INVESTMENT_INCOME, ("Dividend Income", "Investment Income",
                                      "Income from Investments")),
    Mapping(Field.OTHER_INCOME, ("Other Income", "Other Revenue",
                                 "Miscellaneous Income")),
    Mapping(Field.ACCUM_DEPRECIATION, ("Accumulated Depreciation",
                                       "Less Accumulated Depreciation",
                                       "Allowance for Doubtful Accounts",
                                       "Allowance for Doubtful Account")),
    Mapping(Field.INTANGIBLES, ("Intangible Assets", "Intangibles")),
    Mapping(Field.GOODWILL, ("Goodwill",)),
    Mapping(Field.TOTAL_LIABILITIES, ("Total Liabilities",)),
    Mapping(Field.TOTAL_CURRENT_LIABILITIES, ("Total Current Liabilities",)),
    Mapping(Field.TOTAL_NONCURRENT_LIABILITIES, ("Total Long-Term Liabilities",
                                                 "Total Non-Current Liabilities",
                                                 "Total Noncurrent Liabilities")),
    Mapping(Field.ACCOUNTS_PAYABLE, ("Accounts Payable", "Trade Payables")),
    Mapping(Field.ACCRUED_LIABILITIES, ("Accrued Liabilities", "Accrued Expenses",
                                        "Accruals")),
    Mapping(Field.SHORT_TERM_DEBT, ("Short-Term Debt", "Current Portion of Long-Term Debt",
                                    "Line of Credit")),
    Mapping(Field.LONG_TERM_DEBT, ("Long-Term Debt", "Notes Payable")),
    Mapping(Field.EQUITY, ("Total Equity", "Total Stockholders Equity",
                           "Total Shareholders Equity", "Stockholders Equity",
                           "Shareholders Equity", "Owners Equity")),
    Mapping(Field.RETAINED_EARNINGS, ("Retained Earnings", "Accumulated Deficit")),
    Mapping(Field.CAPITAL_STOCK, ("Common Stock", "Capital Stock")),
    Mapping(Field.REVENUE, ("Total Revenues", "Total Revenue", "Revenues", "Revenue",
                            "Net Revenues", "Sales", "Total Sales")),
    Mapping(Field.COST_OF_REVENUE, ("Cost of Sales", "Cost of Revenue",
                                    "Total Cost of Sales", "Cost of Goods Sold",
                                    "Direct Costs")),
    Mapping(Field.GROSS_PROFIT, ("Gross Profit", "Gross Margin")),
    Mapping(Field.OPERATING_INCOME, ("Operating Income", "Income from Operations")),
    Mapping(Field.PRETAX_INCOME, ("Income Before Taxes", "Income Before Income Taxes",
                                  "Profit Before Tax")),
    Mapping(Field.INCOME_TAX, ("Income Tax Expense", "Provision for Income Taxes",
                               "Income Taxes")),
    Mapping(Field.NET_INCOME, ("Net Income", "Net Income (Loss)", "Net Profit",
                               "Net Earnings")),
    Mapping(Field.OPERATING_EXPENSES, ("Total Expenses", "Total Operating Expenses",
                                       "Total Costs and Expenses")),
    Mapping(Field.ADMIN_EXPENSE, ("Salaries", "Salaries and Wages", "Payroll",
                                  "Employee Bonus", "Payroll Tax", "Payroll Taxes",
                                  "Employee Benefit Programs", "Total Direct Comp Cost",
                                  "Rent or Lease", "Office", "Telephone")),
    Mapping(Field.SGNA, ("Selling, General and Administrative",
                         "Selling General and Administrative", "SG&A",
                         "General and Administrative")),
    Mapping(Field.TOTAL_NONCURRENT_LIABILITIES,
            ("Total Long Term Liabilies",)),          # 原表的拼写错误，照收
    Mapping(Field.TOTAL_EQUITY_AND_LIABILITIES,
            ("Total Liabilities and Capital", "Total Liabilities and Equity")),
    Mapping(Field.CAPITAL_STOCK, ("Member's Contribution", "Members Contribution",
                                  "Member Contribution", "Paid-In Capital",
                                  "Paid in Capital", "Capital Contribution")),
    Mapping(Field.RETAINED_EARNINGS, ("Current Year Profit", "Prior Year Profit",
                                      "Accumulated Profit", "Accumulated Loss")),
    Mapping(Field.EQUITY, ("Total Capital", "Partners Capital", "Members Equity")),
    Mapping(Field.LONG_TERM_DEBT, ("Auto Loans", "Vehicle Loans",
                                   "Note Payable", "Bank Loan")),
    Mapping(Field.DEFERRED_REVENUE, ("Deferred Revenue", "Deferred Income",
                                     "Unearned Revenue")),
    Mapping(Field.TAXES_PAYABLE, ("Federal Payroll Taxes Payable", "Other Taxes Payable",
                                  "Sales Tax Payable", "Payroll Taxes Payable",
                                  "Income Taxes Payable")),
    Mapping(Field.EMPLOYEE_PAYABLE, ("PTO Payable", "Flex Deductions Payable",
                                     "Employee Benefits Payable", "Accrued Payroll",
                                     "Wages Payable")),
    Mapping(Field.OTHER_NONCURRENT_ASSETS, ("Other Assets", "Deposits",
                                            "Investment in WSource", "Loan Fees",
                                            "Security Deposits")),
    Mapping(Field.OTHER_CURRENT_ASSETS, ("Medical Claims", "Other Current Assets")),
    Mapping(Field.NET_CURRENT_ASSETS, ("Net Working Capital", "Net Current Assets")),
    Mapping(Field.SECTION, ("Current Assets", "Current Liabilities", "Capital",
                            "Long Term Liabilities", "Other Assets",
                            "LIABILITIES AND CAPITAL", "Balance Sheets Continued",
                            "Property and Equipment", "Current Portion of Long Term Debt")),
)

#: **前缀匹配** —— 真实材料里的标签会被**截断**或带尾巴。
#:
#: 实测 某美国公司 那份资产负债表：
#:
#:     Accum. Depreciation - Furnitur        ← 截断了
#:     Accum. Depreciation - Equipmen
#:     Accum. Depreciation - Automobi
#:     Accum. Depreciation - Leasehol
#:
#: 四个都是累计折旧的三个分项，但精确匹配一个都对不上。
#: 只放**长且无歧义**的前缀 —— 短前缀会误伤。
_PREFIXES: tuple[tuple[str, Field], ...] = (
    ("accumdepreciation", Field.ACCUM_DEPRECIATION),
    ("accumulateddepreciation", Field.ACCUM_DEPRECIATION),
    ("allowancefordoubtful", Field.ACCUM_DEPRECIATION),
    ("accumamort", Field.ACCUM_DEPRECIATION),
    ("accruedexpense", Field.ACCRUED_LIABILITIES),
    ("accruedliabilit", Field.ACCRUED_LIABILITIES),
    ("note payable", Field.LONG_TERM_DEBT),
)

#: **每个字段属于哪张表** —— 用来挡住"串表"。
#:
#: ## 为什么必须挡（实测踩到，差 750 亿）
#:
#: 某白酒公司年报第 61 页同时装着两样东西：**母公司资产负债表的尾巴**
#: （`所有者权益(或股东权益)合计 178,999,246,453.61`）和**合并利润表的开头**。
#:
#: 而利润表的页范围是 61–64 —— `_fill` 把第 61 页的行全吃了，于是
#: **资产负债表的科目混进了利润表**，报出一个 1790 亿的「所有者权益」
#: （真值 2540 亿）。**勾稽不会不平**，因为这个字段根本不参与勾稽。
#:
#: 所以：填某张表时，只接受属于那张表的字段。**被挡掉的要报出来**，
#: 不能默默丢 —— 那可能说明页范围本身就划错了。
#:
#: `ANY` 的字段是确实会跨表出现的：现金流量表的间接法调节段里有「净利润」，
#: 附注里有「折旧摊销」，等等。
ANY = "any"

STATEMENT_OF: dict[str, str] = {}


def _mark(kind: str, *names: str) -> None:
    for n in names:
        STATEMENT_OF[n] = kind


_mark("balance",
      "TOTAL_ASSETS", "TOTAL_CURRENT_ASSETS", "TOTAL_NONCURRENT_ASSETS",
      "SHORT_TERM_INVESTMENTS", "ACCOUNTS_RECEIVABLE", "NOTES_RECEIVABLE",
      "PREPAID_EXPENSES", "INVENTORY", "OTHER_CURRENT_ASSETS",
      "PPE", "PPE_GROSS", "ACCUM_DEPRECIATION", "INTANGIBLES", "GOODWILL",
      "OTHER_NONCURRENT_ASSETS", "TOTAL_LIABILITIES", "TOTAL_CURRENT_LIABILITIES",
      "TOTAL_NONCURRENT_LIABILITIES", "ACCOUNTS_PAYABLE", "NOTES_PAYABLE",
      "TAXES_PAYABLE", "EMPLOYEE_PAYABLE", "DIVIDENDS_PAYABLE",
      "ACCRUED_LIABILITIES", "OTHER_CURRENT_LIABILITIES", "SHORT_TERM_DEBT",
      "LONG_TERM_DEBT", "DEFERRED_REVENUE", "OTHER_NONCURRENT_LIABILITIES",
      "CAPITAL_STOCK", "CAPITAL_RESERVE", "SURPLUS_RESERVE",
      "GENERAL_RISK_RESERVE", "RETAINED_EARNINGS", "TREASURY_STOCK",
      "MINORITY_INTEREST", "EQUITY_PARENT", "EQUITY",
      "TOTAL_EQUITY_AND_LIABILITIES", "NET_ASSETS",
      "ASSETS_LESS_CURRENT_LIABILITIES", "NET_CURRENT_ASSETS",
      # ⚠️ CASH 是资产负债表科目，但现金流量表也有「现金及现金等价物」概念。
      # 这里按**主要归属**放资产负债表 —— 现金流量表的期初/期末用
      # CASH_BEGIN / CASH_END 单独表示。
      "CASH")

_mark("income",
      "REVENUE", "COST_OF_REVENUE", "GROSS_PROFIT", "TAX_SURCHARGE",
      "SELLING_EXPENSE", "ADMIN_EXPENSE", "SGNA", "RND", "OPERATING_EXPENSES",
      "OPERATING_INCOME", "NONOPERATING_INCOME", "NONOPERATING_EXPENSE",
      "INVESTMENT_INCOME", "OTHER_INCOME", "PRETAX_INCOME", "INCOME_TAX",
      "ID_DA", "ID_STOCK_COMP")

_mark("cash_flow",
      "CFO", "CFI", "CFF", "CAPEX", "CASH_BEGIN", "CASH_END",
      "NET_CASH_CHANGE", "FX_EFFECT",
      # 间接法调节段里的营运资本变动
      "ID_WORKING_CAPITAL")

#: 确实会跨表出现的 —— 不挡
_mark(ANY, "SECTION", "OCI", "NET_INCOME", "DEPRECIATION_AMORTIZATION",
      "INTEREST_INCOME", "INTEREST_EXPENSE")


def field_statement(f) -> str:
    """这个字段属于哪张表。认不出返回 `ANY`（不挡）。"""
    return STATEMENT_OF.get(getattr(f, "name", str(f)), ANY)


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
    (("期初", "年初", "beginning"), Field.CASH_BEGIN),
    (("期末", "年末", "年终", "endofperiod", "atend", "attheend"), Field.CASH_END),
)


def _norm(s: str) -> str:
    """比对用的归一化。

    ## 括号里的内容要去掉（实测踩到）

    A 股年报里权益行写的是**「所有者权益(或股东权益)合计」** ——
    括号里是别名。原来的归一化只去标点、留下内容，于是
    `所有者权益或股东权益合计` 匹配不上表里的 `所有者权益合计`，
    整个勾稽**判不了**。

    同类的还有「负债和所有者权益(或股东权益)总计」。

    去掉括号内容对匹配普遍有利：「固定资产(净额)」→「固定资产」也更能对上。

    另外剥掉「减:」「其中:」这类前缀 —— 它们是排版用的，不是科目名的一部分。
    """
    s = s.strip().lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    # 括号及其内容：全角半角都要处理
    # **但如果整行都在括号里**（如「（货币资金）」），剥完就空了 —— 退回原文。
    stripped = re.sub(r"[（(][^（()）]*[)）]", "", s)
    if stripped.strip():
        s = stripped
    else:
        s = re.sub(r"[（()）]", "", s)
    # 排版前缀
    # 「一、」「二、」这类序号是 A 股报表的标准排版 —— 不剥掉的话
    # 「一、营业总收入」「四、汇率变动对现金的影响」全都匹配不上。
    s = re.sub(r"^[一二三四五六七八九十]+[、.]", "", s)
    s = re.sub(r"^(其中|减|加|其中：|减：|加：)[:：]?", "", s)
    s = re.sub(r"[（()）【】\[\]：:，,、。.\-—_*“”\"']", "", s)
    # 「损失以“-”号填列」这类填表说明是格式要求，不是科目名的一部分
    s = re.sub(r"损失以.*?号填列", "", s)
    s = re.sub(r"亏损以.*?号填列", "", s)
    return s


# 预先建索引，避免每次线性扫描
_BY_TAG: dict[str, Field] = {}
_BY_NAME: dict[str, Field] = {}
for _m in MAPPINGS:
    for _t in _m.tags:
        _BY_TAG.setdefault(_norm(_t), _m.field)
    for _n in _m.names:
        _BY_NAME.setdefault(_norm(_n), _m.field)


#: 繁简转换器。有 zhconv 就用它（完整表），没有就退到内置的小表。
try:
    from zhconv import convert as _zh_convert

    def _to_simplified(s: str) -> str:
        return _zh_convert(s, "zh-cn")

    _HAS_ZHCONV = True

except ImportError:  # pragma: no cover - 取决于环境
    _TRAD_TO_SIMP = str.maketrans({
        "為": "为", "產": "产", "負": "负", "債": "债", "資": "资", "現": "现",
        "務": "务", "業": "业", "應": "应", "計": "计", "總": "总", "額": "额",
        "動": "动", "費": "费", "稅": "税", "項": "项", "營": "营", "潤": "润",
        "損": "损", "權": "权", "東": "东", "聯": "联", "廠": "厂", "設": "设",
        "備": "备", "無": "无", "遞": "递", "預": "预", "貨": "货", "幣": "币",
        "銀": "银", "結": "结", "餘": "余", "帳": "账", "賬": "账", "匯": "汇",
        "兌": "兑", "報": "报", "註": "注", "準": "准", "則": "则", "與": "与",
        "轉": "转", "換": "换", "淨": "净", "長": "长", "間": "间", "屬": "属",
        "發": "发", "投": "投", "購": "购", "處": "处", "終": "终", "減": "减",
        "值": "值", "攤": "摊", "銷": "销", "虧": "亏", "彌": "弥", "補": "补",
        "儲": "储", "餘": "余", "貸": "贷", "壞": "坏", "誤": "误", "審": "审",
        "閱": "阅", "會": "会", "師": "师", "報": "报", "告": "告", "書": "书",
    })

    def _to_simplified(s: str) -> str:
        return s.translate(_TRAD_TO_SIMP)

    _HAS_ZHCONV = False


def _split_bilingual(label: str) -> list[str]:
    """把中英双语连写的标签拆成几段。

    ## 为什么（实测踩到）

    H 股年报的科目名是**中英双语连写**：

        Property, plant and equipment 物業、廠房及設備
        Cash and bank balances 現金及銀行結餘
        Total assets less current liabilities 總資產減流動負債

    整串拿去匹配，简体中文表里一条都对不上 —— 于是整张表「映射上 0 行」。
    拆开分别试才行。
    """
    parts = [p.strip() for p in re.split(
        r"(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z(])|(?<=[A-Za-z)])\s+(?=[\u4e00-\u9fff])",
        label) if p.strip()]
    return parts if len(parts) > 1 else []


def _label_candidates(label: str) -> list[str]:
    """把一行标签拆成若干「候选写法」，依次去匹配科目表。

    顺序 = 从最具体到最泛：原文 → 简体 → 双语拆开（各自再转简体）。
    """
    out: list[str] = []

    def add(s: str) -> None:
        n = _norm(s)
        if n and n not in out:
            out.append(n)

    add(label)
    simp = _to_simplified(label)
    if simp != label:
        add(simp)
    for part in _split_bilingual(label):
        add(part)
        add(_to_simplified(part))
    return out


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
    cands = _label_candidates(label)
    if not cands:
        cands = [lab]

    # 货币资金 vs 期初/期末现金的消歧要在**所有候选写法**上都试，
    # 否则繁体「現金及銀行結餘」进不来。
    if any(any(k in c for k in ("现金", "cash")) for c in cands):
        for keys, f in _DISAMBIGUATING:
            if any(any(k in c for k in keys) for c in cands):
                return f, "label-disambiguated"

    if xbrl_tag:
        raw = xbrl_tag.split(":")[-1]
        if raw.endswith(_ABSTRACT_SUFFIX):
            return Field.SECTION, "tag"
        hit = _BY_TAG.get(_norm(raw))
        if hit is not None:
            return hit, "tag"

    if label.strip():
        # **逐个候选写法试**：原文 → 简体 → 双语拆开。
        # H 股年报里 `Property, plant and equipment 物業、廠房及設備`
        # 只有拆开再转简体才匹配得上。
        for c in cands:
            hit = _BY_NAME.get(c)
            if hit is not None:
                return hit, "name"
        # 精确匹配全落空，才试**前缀** —— 标签被截断的情况（见 `_PREFIXES`）
        for c in cands:
            for pre, f in _PREFIXES:
                if c.startswith(pre):
                    return f, "prefix"

    return None, "none"
