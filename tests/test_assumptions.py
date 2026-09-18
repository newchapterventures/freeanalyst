"""DCF 假设清单的测试。

重点不是「值填得对不对」，是**清单本身有没有守住纪律**：
每条都给依据和坑、缺数据不拿默认值填、收入增速必须指回外部数据。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valuation import assumptions as A  # noqa: E402


class TestChecklistIntegrity(unittest.TestCase):
    def test_has_the_seven_main_ones(self):
        """用户点名的七个主要假设一个都不能少。"""
        keys = {a.key for a in A.DCF_ASSUMPTIONS}
        for k in ("revenue_growth", "terminal_growth", "ebitda_margin",
                  "da_pct", "capex_pct", "nwc_pct", "wacc"):
            self.assertIn(k, keys, k)

    def test_has_the_others_the_user_mentioned(self):
        """用户说「还会有一些其他的」—— 这些桥接项容易被漏。"""
        keys = {a.key for a in A.DCF_ASSUMPTIONS}
        for k in ("net_debt_scope", "minority_interest", "sbc",
                  "nonrecurring", "midyear", "exit_method"):
            self.assertIn(k, keys, k)

    def test_every_assumption_explains_itself(self):
        """**每条都要给四样东西**：影响什么、怎么校验、最容易拍错、数据从哪来。

        清单的价值不在「列了哪些假设」（那是常识），
        在「每条都告诉你这个数字该怎么校验、最容易错在哪」。
        """
        for a in A.DCF_ASSUMPTIONS:
            with self.subTest(key=a.key):
                self.assertTrue(a.drives, a.key)
                self.assertTrue(a.check, a.key)
                self.assertTrue(a.trap, a.key)
                self.assertIsInstance(a.need, A.Need)

    def test_trap_is_specific_not_generic(self):
        """坑要具体到「会怎么错」，不能是「要小心」这种废话。"""
        for a in A.DCF_ASSUMPTIONS:
            with self.subTest(key=a.key):
                self.assertGreater(len(a.trap), 30, a.key)

    def test_keys_unique(self):
        keys = [a.key for a in A.DCF_ASSUMPTIONS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_groups_cover_everything(self):
        for a in A.DCF_ASSUMPTIONS:
            self.assertIn(a.group, list(A.Group), a.key)


class TestNoDefaultFill(unittest.TestCase):
    """**缺数据就说缺。** 用默认值填出来的清单看起来完整，
    但会让用户以为某项已经定了，实际是个假数。"""

    def test_no_statements_leaves_values_empty(self):
        items = A.build_checklist(None)
        for a in items:
            self.assertIsNone(a.value, f"{a.key} 不该有值")
            self.assertIsNone(a.user_value, a.key)

    def test_revenue_growth_points_at_comps(self):
        """用户明确要求：**收入增速要参考外部数据（可比公司）**。"""
        items = {a.key: a for a in A.build_checklist(None, comps_ready=False)}
        self.assertIs(items["revenue_growth"].need, A.Need.EXTERNAL)
        self.assertTrue(items["revenue_growth"].external_need)
        self.assertTrue(items["revenue_growth"].external_gap)

    def test_comps_ready_clears_the_gap(self):
        items = {a.key: a for a in A.build_checklist(None, comps_ready=True)}
        self.assertEqual(items["revenue_growth"].external_gap, "")

    def test_build_does_not_mutate_the_table(self):
        """`build_checklist` 不能改到全局表 —— 第二次调用会带上一轮的值。"""
        A.build_checklist(None)
        for a in A.DCF_ASSUMPTIONS:
            self.assertIsNone(a.value, f"{a.key} 被写脏了")
            self.assertIsNone(a.user_value, a.key)


class TestRender(unittest.TestCase):
    def test_lists_everything(self):
        t = A.render_checklist(A.build_checklist(None))
        for a in A.DCF_ASSUMPTIONS:
            self.assertIn(a.label, t)

    def test_marks_what_needs_the_user(self):
        t = A.render_checklist(A.build_checklist(None))
        self.assertIn("要你定", t)

    def test_counts_at_the_end(self):
        t = A.render_checklist(A.build_checklist(None))
        self.assertIn("需要你确认或给值", t)

    def test_has_four_groups(self):
        t = A.render_checklist(A.build_checklist(None))
        for g in A.Group:
            self.assertIn(g.value, t, g.value)


class TestFillFromStatements(unittest.TestCase):
    """用合成材料试填 —— 不走真实文件，测的是**填的逻辑**。"""

    class _St:
        def __init__(self, fields):
            self.fields = fields

    class _S:
        def __init__(self, bal, inc, cf, da=None):
            self.balance = TestFillFromStatements._St(bal)
            self.income = TestFillFromStatements._St(inc)
            self.cash_flow = TestFillFromStatements._St(cf)
            self.da = da
            self.warnings = []

    def _run(self, S):
        return {a.key: a for a in A.build_checklist(S)}

    def test_tax_rate_from_statements(self):
        from financials.canonical import Field as F
        S = self._S({}, {F.INCOME_TAX: 25.0, F.PRETAX_INCOME: 100.0}, {})
        by = self._run(S)
        self.assertAlmostEqual(by["tax_rate"].value, 0.25)
        self.assertIn("所得税费用", by["tax_rate"].basis)

    def test_tax_rate_missing_stays_empty(self):
        by = self._run(self._S({}, {}, {}))
        self.assertIsNone(by["tax_rate"].value)
        self.assertIn("取不到", by["tax_rate"].basis)

    def test_nwc_uses_balance_sheet_only(self):
        from financials.canonical import Field as F
        S = self._S({F.ACCOUNTS_RECEIVABLE: 200.0, F.INVENTORY: 100.0,
                     F.ACCOUNTS_PAYABLE: 50.0}, {F.REVENUE: 1000.0}, {})
        by = self._run(S)
        self.assertAlmostEqual(by["nwc_pct"].value, 0.25)
        # 要提醒这是存量占比，不是 ΔNWC
        self.assertIn("两期之差", by["nwc_pct"].basis)

    def test_minority_interest_read_from_balance_sheet(self):
        from financials.canonical import Field as F
        by = self._run(self._S({F.MINORITY_INTEREST: 1844109.0}, {}, {}))
        self.assertEqual(by["minority_interest"].value, 1844109.0)

    def test_no_minority_interest_says_why(self):
        by = self._run(self._S({}, {}, {}))
        self.assertIsNone(by["minority_interest"].value)
        self.assertIn("没有少数股东权益科目", by["minority_interest"].basis)

    def test_da_missing_explains_the_notes_path(self):
        by = self._run(self._S({}, {}, {}))
        self.assertIsNone(by["da_pct"].value)
        self.assertIn("将净利润调节为经营活动现金流量", by["da_pct"].basis)


if __name__ == "__main__":
    unittest.main(verbosity=2)
