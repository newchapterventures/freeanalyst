"""乘数法的基数**不能为负** —— 拒绝得对，但不该表现成"崩"。

## 实测（一份 10-K）
EBITDA 是 **−74,332** → 引擎拒绝：

    ✗ 算不出来：EBITDA 为 -74332.0，不能作为乘数基数。
       负或零基数的标的应改用其他方法（早期项目方法或资产法）。

**引擎拒绝是对的** —— 乘数 × 负数 = 负价值，没有意义，
而且"给一个负的价值"比"不给"更坏。

问题在于它表现成**崩** ✗：整条链被打断、连报告都出不来。
按项目规矩该是"**整块不跑 + 说清为什么 + 给出替代口径**" ——
这也是这一层（`_multiples_base_problem`）存在的理由。

## 边界
· 只有 EBITDA / EBIT / SDE 这类**可能为负的利润类指标**才判；
  **收入**不可能为负，不该被这条挡住（挡了就是误报）。
· 值为 None（材料里推不出）走**另一条**报缺路径，不在这里重复报。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import intake as it                                        # noqa: E402


class TestMultiplesBase(unittest.TestCase):
    def test_negative_ebitda_is_a_problem_with_an_alternative(self):
        msg = it._multiples_base_problem("EBITDA", "ebitda", -74332.0)
        self.assertIn("负", msg)
        self.assertIn("收入", msg)          # **必须给出路**
        self.assertIn("早期项目", msg)

    def test_zero_is_also_unusable(self):
        self.assertTrue(it._multiples_base_problem("EBITDA", "ebitda", 0.0))

    def test_positive_base_is_fine(self):
        self.assertEqual(it._multiples_base_problem("EBITDA", "ebitda", 12345.0), "")

    def test_revenue_is_never_blocked(self):
        """收入不可能为负 —— 拿这条去挡收入就是误报。"""
        self.assertEqual(it._multiples_base_problem("收入", "revenue", 100.0), "")

    def test_missing_value_is_handled_elsewhere(self):
        """值为 None 走另一条报缺路径，这里不重复报（免得一句话说两遍）。"""
        self.assertEqual(it._multiples_base_problem("EBITDA", "ebitda", None), "")


if __name__ == "__main__":
    unittest.main()
