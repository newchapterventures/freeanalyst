"""净债务：**推得出来就不问，推不出来必须问**。

## 为什么这条要有守卫（实测：全链条在这里断）
引擎对"核心输入缺失"是**硬报错**的 —— 那条规矩是对的：
净债务偏低会让股权价值偏高，**往好看的方向偏**，比不跑更坏。
问题是**用户没有地方能填它** ✗：问答清单里原本没有这一行，
于是材料里取不到借款/现金科目时，整条链出不来报告。

所以规则是：
  · 材料推得出来 → **不问**（能确定的事不让人打字）
  · 推不出来 → **问一行**（把原因写在参考里，用户知道为什么缺）
  · 两边都没有 → DCF **整块不跑**，并在报告里说清缺什么（而不是崩）

`_net_debt_derived` 就是这条规则的判据，这里把它钉住。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import intake as it                                        # noqa: E402
from financials import statements as stm                   # noqa: E402
from financials.canonical import Field                     # noqa: E402


class _FakeMat:
    def __init__(self, fields):
        S = stm.Statements(gaap="", scope="", audited="", period="", unit="元")
        if fields is not None:
            S.balance = stm.StatementSet(name="balance", source="test")
            S.balance.fields = fields
        self.statements = S


class TestNetDebtDerived(unittest.TestCase):
    def test_derivable_when_debt_and_cash_are_there(self):
        # 有息负债 = 短期借款 + 长期借款（**两个都要有**，只给一个属于设计上的"报缺"情形）
        mat = _FakeMat({Field.SHORT_TERM_DEBT: 1000.0, Field.LONG_TERM_DEBT: 0.0,
                        Field.CASH: 300.0})
        val, why = it._net_debt_derived(mat)
        self.assertEqual(val, 700.0, f"推得出来却报缺（{why}）")

    def test_not_derivable_without_a_balance_sheet(self):
        val, why = it._net_debt_derived(_FakeMat(None))
        self.assertIsNone(val)
        self.assertIn("资产负债表", why)

    def test_not_derivable_when_no_debt_and_no_cash(self):
        """材料里既没有借款也没有现金科目 → 推不出来，**必须问用户**。"""
        val, why = it._net_debt_derived(_FakeMat({Field.TOTAL_LIABILITIES: 100.0}))
        self.assertIsNone(val)
        self.assertTrue(why, "推不出来时要给出原因，不能只说'缺'")

    def test_partial_debt_is_reported_missing_not_guessed(self):
        """**只找到部分借款科目 → 报缺，不许猜。** 按 0 处理会让净债务偏低、
        股权价值偏高 —— 往看起来更好的方向偏。"""
        mat = _FakeMat({Field.SHORT_TERM_DEBT: 1000.0, Field.CASH: 300.0,
                        Field.TOTAL_LIABILITIES: 5000.0,
                        Field.ACCOUNTS_PAYABLE: 4000.0})
        val, why = it._net_debt_derived(mat)
        self.assertIsNone(val)
        self.assertIn("借款", why)

    def test_the_predicate_matches_the_question_rule(self):
        """判据只有一份：`nd_ask` 用的是同一个函数（问了不该问的，会白折腾用户）。"""
        mat = _FakeMat({Field.SHORT_TERM_DEBT: 1000.0, Field.LONG_TERM_DEBT: 0.0,
                        Field.CASH: 300.0})
        val, _ = it._net_debt_derived(mat)
        self.assertTrue(val is not None)      # → 这个情形下不问


if __name__ == "__main__":
    unittest.main()
