"""「串表」防护与字段冲突裁决的测试。

## 这里防的是最阴的一类错

**数字都挺像样、勾稽也平，但分子分母不是同一个主体。**
实测某白酒公司年报差 750 亿，就是因为页范围扫到了相邻表的尾巴。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from financials import canonical as cn  # noqa: E402
from financials import from_pdf  # noqa: E402
from financials import statements as stm  # noqa: E402


class TestStatementOwnership(unittest.TestCase):
    def test_balance_fields(self):
        for name in ("EQUITY", "TOTAL_ASSETS", "TOTAL_LIABILITIES",
                     "CASH", "ACCOUNTS_RECEIVABLE", "RETAINED_EARNINGS"):
            self.assertEqual(cn.field_statement(cn.Field[name]), "balance", name)

    def test_income_fields(self):
        for name in ("REVENUE", "COST_OF_REVENUE", "OPERATING_INCOME",
                     "PRETAX_INCOME", "INCOME_TAX"):
            self.assertEqual(cn.field_statement(cn.Field[name]), "income", name)

    def test_cash_flow_fields(self):
        for name in ("CFO", "CFI", "CFF", "CAPEX", "CASH_BEGIN", "CASH_END"):
            self.assertEqual(cn.field_statement(cn.Field[name]), "cash_flow", name)

    def test_cross_statement_fields_are_any(self):
        """**确实跨表的字段不许挡。**

        现金流量表的间接法调节段里有「净利润」，附注里有「折旧摊销」——
        挡掉它们会把合法数据一起挡掉。
        """
        for name in ("NET_INCOME", "DEPRECIATION_AMORTIZATION", "SECTION",
                     "INTEREST_INCOME", "INTEREST_EXPENSE"):
            self.assertEqual(cn.field_statement(cn.Field[name]), cn.ANY, name)

    def test_every_field_is_mapped(self):
        """每个字段都得有归属，**不许有漏网的**。

        漏了的会被当成 ANY 放行 —— 那就等于没挡。
        """
        missing = [f.name for f in cn.Field
                   if f.name not in cn.STATEMENT_OF]
        self.assertEqual(missing, [], f"这些字段没归属：{missing}")


class TestConflictResolution(unittest.TestCase):
    def _st(self):
        return stm.StatementSet(name="资产负债表", source="test")

    def test_single_value_no_conflict(self):
        st = self._st()
        st.fields[cn.Field.INVENTORY] = 100.0
        from_pdf._resolve_conflicts(st, {cn.Field.INVENTORY: [(100.0, 57, True)]})
        self.assertEqual(st.conflicts, [])

    def test_first_occurrence_wins(self):
        """**取首次出现，不按「是不是合并页」判。**

        试过更聪明的规则（按页判断合并 / 母公司，优先取合并那个）—— 不行。
        母公司利润表里也有「归属于母公司所有者的净利润」，一样会被标成合并页，
        一个「合并」标志在两类页上都成立，**区分度为零**。
        实测把 `营业收入` 从 172,054,171,890.91（合并）改判成了
        98,318,530,088.73（母公司），**方向正好反了**。

        可靠的是文档结构：**中文年报里合并表永远排在母公司表前面**。
        """
        st = self._st()
        st.fields[cn.Field.REVENUE] = 172_054_171_890.91
        from_pdf._resolve_conflicts(
            st, {cn.Field.REVENUE: [(172_054_171_890.91, 61, False),   # 合并，先出现
                                    (98_318_530_088.73, 63, True)]})   # 母公司
        self.assertEqual(st.fields[cn.Field.REVENUE], 172_054_171_890.91)

    def test_other_value_is_reported_not_dropped(self):
        """另一个取值**不许丢** —— 要报出来让人能核对。"""
        st = self._st()
        st.fields[cn.Field.REVENUE] = 1.0
        from_pdf._resolve_conflicts(
            st, {cn.Field.REVENUE: [(1.0, 61, True), (2.0, 63, True)]})
        self.assertIn("2.00", st.conflicts[0])
        self.assertIn("第 63 页", st.conflicts[0])

    def test_the_750_billion_case(self):
        """**回归：差 750 亿的那个字段。**

        某白酒公司年报里 `所有者权益(或股东权益)合计` 出现两次：
            第 59页 合并资产负债表         253,959,253,909.07
            第 61页 母公司资产负债表的尾巴   178,999,246,453.61
        而利润表的页范围是 61–64。

        修复前：1790 亿（错的那个，而且勾稽不会不平）。
        修复后：2540 亿（合并口径）。
        """
        st = self._st()
        st.fields[cn.Field.EQUITY] = 253_959_253_909.07
        from_pdf._resolve_conflicts(
            st, {cn.Field.EQUITY: [(253_959_253_909.07, 59, True),
                                   (178_999_246_453.61, 61, False)]})
        self.assertEqual(st.fields[cn.Field.EQUITY], 253_959_253_909.07)


class TestForeignRowGuard(unittest.TestCase):
    def test_balance_field_blocked_from_income(self):
        """资产负债表科目不许进利润表。"""
        self.assertEqual(cn.field_statement(cn.Field.EQUITY), "balance")
        self.assertNotIn(cn.field_statement(cn.Field.EQUITY), ("income", cn.ANY))

    def test_income_field_blocked_from_balance(self):
        self.assertEqual(cn.field_statement(cn.Field.REVENUE), "income")
        self.assertNotIn(cn.field_statement(cn.Field.REVENUE), ("balance", cn.ANY))


if __name__ == "__main__":
    unittest.main(verbosity=2)
