"""链断了要给**能动手的话**，不是一段栈。

## 实测（全链条冒烟测试踩到）
那份材料的问答清单里 `debt` 没填 → 净债务推不出来 → 引擎抛
`ValueError: 假设「净债务」缺失（来源：未提供），无法继续计算` →
**用户面对的是一段栈** ✗，而"缺假设"本来是 10 秒能补上、可动手的事。

## 两条界线
1. **缺假设 ≠ 引擎出错**：前者要指路（填哪一行），后者要照实说"这是我们的问题"
   并留下栈便于定位 —— 不能把引擎 bug 伪装成"材料缺数"。
2. **认不出的假设不许硬编**：映射表里没有的就只报原话，不猜一个"应该是某某"。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import intake as it                                        # noqa: E402


class TestMissingHint(unittest.TestCase):
    def test_net_debt_points_to_the_debt_line(self):
        """实测就断在这一条上。"""
        hint = it._missing_hint("假设「净债务」缺失（来源：未提供），无法继续计算")
        self.assertIn("debt", hint)
        self.assertIn("0", hint)

    def test_external_inputs_point_to_their_keys(self):
        for name, key in (("无风险利率", "risk_free"),
                          ("股权风险溢价", "equity_risk_premium"),
                          ("去杠杆 beta", "beta_unlevered"),
                          ("永续增长率", "terminal_growth")):
            hint = it._missing_hint(f"假设「{name}」缺失（来源：未提供），无法继续计算")
            self.assertIn(key, hint, name)

    def test_unknown_assumption_returns_nothing(self):
        """**不硬编** —— 认不出就返回空，让上层只报原话。"""
        self.assertEqual(it._missing_hint("假设「某某新指标」缺失，无法继续计算"), "")
        self.assertEqual(it._missing_hint("完全不相干的一句话"), "")


if __name__ == "__main__":
    unittest.main()
