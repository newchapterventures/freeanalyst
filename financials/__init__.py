"""三张表 → 标准科目 + 勾稽校验。

    from financials import canonical, articulation

    字段, 依据 = canonical.identify("货币资金", "us-gaap:CashAndCashEquivalentsAtCarryingValue")

校验接口是纯函数，吃 `{Field: 数值}`，不依赖 IO：

    articulation.check_balance(bal)
    articulation.check_cash_rollforward(cf, cash_begin, cash_end)
    articulation.check_indirect_method(rows, net_income, cfo)
"""

from . import articulation, canonical, derive  # noqa: F401

__all__ = ["articulation", "canonical", "derive"]
