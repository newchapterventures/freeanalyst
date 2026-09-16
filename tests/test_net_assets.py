"""IFRS「净资产列报式」的校验测试。

## 为什么单独有一类

A 股的资产负债表长这样：

    资产总计 = 负债合计 + 所有者权益合计

H 股（IFRS）常用的是**净资产列报式**，**根本没有上面那两行**：

    非流动资产 … 小计
    流动资产   … 小计
    流动负债   … 小计
    流动资产净额            = 流动资产 − 流动负债
    总资产减流动负债         = 非流动资产 + 流动资产净额
    非流动负债 … 小计
    资产净额               = 总资产减流动负债 − 非流动负债
    权益总额               = 资产净额

所以 `资产 = 负债 + 权益` 这条检查**用不了** —— 但这不是「数据缺了」，
是**格式不同**。必须报「不适用」而不是让用户以为漏了材料，
同时用等价的 `资产净额 = 权益总额` 顶上。

取材自某 H 股公司 2020 年报（H 股）真实的财务状况表。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import articulation as art          # noqa: E402
from financials import statements as stm            # noqa: E402
from financials.canonical import Field              # noqa: E402


def _baotree_balance() -> dict[Field, float]:
    """某 H 股公司 2020 财务状况表的关键行（千元，实测值）。"""
    return {
        Field.NET_CURRENT_ASSETS: 1_887_814,
        Field.ASSETS_LESS_CURRENT_LIABILITIES: 2_317_397,
        Field.NET_ASSETS: 2_314_754,
        Field.EQUITY: 2_314_754,
        Field.EQUITY_PARENT: 2_311_864,
        Field.MINORITY_INTEREST: 2_890,
        Field.TOTAL_NONCURRENT_LIABILITIES: 2_643,
    }


def _a_share_balance() -> dict[Field, float]:
    """A 股格式的关键行（某白酒公司 2025，元）。"""
    return {
        Field.TOTAL_ASSETS: 303_834_844_021.44,
        Field.TOTAL_LIABILITIES: 49_875_590_112.37,
        Field.EQUITY: 253_959_253_909.07,
        Field.MINORITY_INTEREST: 9_321_442_876.89,
    }


class TestDetectPresentation(unittest.TestCase):
    def test_net_asset_presentation(self):
        self.assertTrue(art.is_net_asset_presentation(_baotree_balance()))

    def test_a_share_is_not_net_asset(self):
        """A 股有「资产总计」「负债合计」，不是这个格式。"""
        self.assertFalse(art.is_net_asset_presentation(_a_share_balance()))

    def test_needs_both_rows(self):
        """只有资产净额、没有权益总额 —— 判不了，不能算这种格式。"""
        self.assertFalse(art.is_net_asset_presentation(
            {Field.NET_ASSETS: 100.0}))


class TestCheckNetAssets(unittest.TestCase):
    def test_balances(self):
        r = art.check_net_assets(_baotree_balance())
        self.assertTrue(r.ok, r.render())
        self.assertEqual(r.diff, 0)

    def test_caught_when_parent_equity_used_by_mistake(self):
        """**这是实测踩到的那一跤。**

        `Total equity attributable to equity shareholders of the Company`
        （母公司权益 2,311,864）被错当成 `权益总额`，差正好是一个
        少数股东权益（2,890）。这条校验就是用来抓这个的。
        """
        bal = _baotree_balance()
        bal[Field.EQUITY] = 2_311_864          # 错用母公司权益
        r = art.check_net_assets(bal)
        self.assertFalse(r.ok)
        self.assertEqual(r.diff, 2_890)

    def test_cross_checks_against_assets_less_current_liabilities(self):
        """有部件时顺带用「总资产减流动负债 − 非流动负债」旁证一遍。"""
        r = art.check_net_assets(_baotree_balance())
        self.assertIn("旁证", r.note)
        self.assertIn("2,314,754", r.note)
        self.assertIn("一致", r.note)

    def test_cross_check_does_not_decide_ok(self):
        """**旁证不能决定 `ok`。**

        实测踩到：`ok` 一度由旁证那一侧算出来。于是「母公司权益被错当成
        权益总额」时，因为旁证仍然对，整条校验显示「平」—— 错的那一栏
        被掩盖了。
        """
        bal = _baotree_balance()
        bal[Field.EQUITY] = 2_311_864          # 错用母公司权益
        r = art.check_net_assets(bal)
        self.assertFalse(r.ok)                 # 主张不成立
        self.assertIn("一致", r.note)          # 旁证仍然对 —— 但不能因此翻案

    def test_reports_missing_not_ok(self):
        r = art.check_net_assets({Field.NET_ASSETS: 100.0})
        self.assertIsNone(r.ok)
        self.assertIn(Field.EQUITY, r.missing)


class TestStatementsChecksDispatch(unittest.TestCase):
    """`Statements.checks()` 要按格式分流。"""

    def _balance_set(self, fields):
        s = stm.StatementSet(name="财务状况表", source="x", unit="千元")
        s.fields = fields
        return s

    def test_net_asset_gets_not_applicable_plus_equivalent(self):
        checks = stm.Statements(
            balance=self._balance_set(_baotree_balance()),
            gaap="IFRS", scope="合并", audited="已审计", period="2020-12-31",
        ).checks()
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[0].status, "不适用")
        self.assertIn("净资产列报式", checks[0].note)
        # 等价校验顶上，而且判得出来
        self.assertTrue(checks[1].ok)

    def test_a_share_runs_the_normal_check(self):
        checks = stm.Statements(
            balance=self._balance_set(_a_share_balance()),
            gaap="CAS", scope="合并", audited="已审计", period="2025-12-31",
        ).checks()
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].ok, checks[0].render())

    def test_not_applicable_is_not_missing_data(self):
        """两者对用户的意义完全不同：前者不用管，后者要去补数据。

        合并过一个 bug：净资产列报式被报成「数据不足」，
        看着像材料缺了两张表。
        """
        r = art.Articulation("x", None, applicable=False, note="格式不同")
        self.assertEqual(r.status, "不适用")
        self.assertNotEqual(r.status, "数据不足")


if __name__ == "__main__":
    unittest.main()
