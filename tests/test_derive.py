"""从三张表推出引擎口径的测试。

## 这里最重要的是"缺数据怎么办"

`net_debt` 和 `ebitda` 都是 DCF 的核心输入，而它们由多个科目拼出来。
拼不全的时候有三种做法：

| 做法 | 后果 |
|---|---|
| 缺的按 0 | 净债务偏低 → 股权价值偏**高**（往好看的方向偏） |
| 用行业平均填 | 得到一个看起来正常、实际无据的数 |
| **报缺** | 用户知道要去补什么 |

选第三种。**往好看的方向偏是最危险的 —— 它不会引起怀疑。**
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import derive as dv  # noqa: E402
from financials.canonical import Field  # noqa: E402


class TestNetDebt(unittest.TestCase):
    def test_basic(self):
        """1,000 借款 − 300 现金 = 700 净债务。"""
        d = dv.net_debt({Field.SHORT_TERM_DEBT: 600.0,
                         Field.LONG_TERM_DEBT: 400.0,
                         Field.CASH: 300.0})
        self.assertEqual(d.value, 700.0)

    def test_parts_are_shown_for_audit(self):
        """**孤零零一个数没法核对。** 要能看到构成。"""
        d = dv.net_debt({Field.SHORT_TERM_DEBT: 600.0,
                         Field.LONG_TERM_DEBT: 400.0,
                         Field.CASH: 300.0})
        self.assertEqual(len(d.parts), 3)
        rendered = d.render()
        self.assertIn("700", rendered)
        self.assertIn("短期借款", rendered)

    def test_zero_debt_company_is_ok(self):
        """零负债公司：现金有、借款科目值为 0 —— 这是能算的，不该报缺。"""
        d = dv.net_debt({Field.SHORT_TERM_DEBT: 0.0,
                         Field.LONG_TERM_DEBT: 0.0,
                         Field.CASH: 300.0})
        self.assertEqual(d.value, -300.0)

    def test_missing_debt_line_is_reported_not_zeroed(self):
        """**只找到短期借款、没找到长期借款时必须报缺。**

        按 0 处理会让净债务偏低 → 股权价值偏高 ——
        往看起来更好的方向偏，而且完全不报错。
        """
        d = dv.net_debt({Field.SHORT_TERM_DEBT: 600.0, Field.CASH: 300.0})
        self.assertIsNone(d.value, "缺长期借款却算出了数")
        self.assertIn(Field.LONG_TERM_DEBT, d.missing)
        self.assertIn("偏高", d.render())

    def test_nothing_found(self):
        d = dv.net_debt({})
        self.assertIsNone(d.value)

    def test_short_term_investments_count_as_cash_equivalent(self):
        d = dv.net_debt({Field.SHORT_TERM_DEBT: 0.0,
                         Field.LONG_TERM_DEBT: 0.0,
                         Field.CASH: 300.0,
                         Field.SHORT_TERM_INVESTMENTS: 200.0})
        self.assertEqual(d.value, -500.0)


class TestMinorityInterest(unittest.TestCase):
    def test_absent_defaults_to_zero(self):
        """这个科目本来就不是每家都有，按 0 处理**不会造成方向性偏差**。"""
        d = dv.minority_interest({})
        self.assertEqual(d.value, 0.0)
        self.assertIn("无此科目", d.note)

    def test_present(self):
        d = dv.minority_interest({Field.MINORITY_INTEREST: 1234.0})
        self.assertEqual(d.value, 1234.0)


class TestEbitda(unittest.TestCase):
    def test_basic(self):
        """营业利润 (100) + 折旧摊销 30 = -70。"""
        d = dv.ebitda({Field.OPERATING_INCOME: -100.0,
                       Field.DEPRECIATION_AMORTIZATION: 30.0})
        self.assertEqual(d.value, -70.0)

    def test_missing_da_is_reported(self):
        """**不许用行业平均折旧率去填。**

        会得到一个看起来正常、实际无据的数，而它还是 DCF 的核心输入。
        """
        d = dv.ebitda({Field.OPERATING_INCOME: 100.0})
        self.assertIsNone(d.value)
        self.assertIn(Field.DEPRECIATION_AMORTIZATION, d.missing)
        self.assertIn("无据", d.render())

    def test_missing_operating_income(self):
        d = dv.ebitda({Field.DEPRECIATION_AMORTIZATION: 30.0})
        self.assertIsNone(d.value)


class TestDeriveAll(unittest.TestCase):
    def test_returns_all_three(self):
        out = dv.derive_all({Field.CASH: 1.0}, {Field.OPERATING_INCOME: 2.0})
        self.assertEqual(len(out), 3)

    def test_without_income_statement(self):
        out = dv.derive_all({Field.CASH: 1.0})
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
