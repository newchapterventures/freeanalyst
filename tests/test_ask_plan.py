"""假设层的**问答计划**（`/api/ask` 的 `plan`）—— 排序逻辑必须可预测、可解释。

## 排序依据：**用户的成本**，不是"重要性"的玄学
    ① `财报推算` —— 工具已有参考值，用户只需点头 = 零成本 → 先清掉
    ② `判断`     —— 要动脑，但影响最大 → 按影响表排（每条带 why）
    ③ `外部`     —— 要去查资料（外部作业）→ 放最后，别打断思路
    ④ `财报`     —— 报表里有的，扫描时一般已填好
同一档内**保持清单原顺序**（每次问的次序都一样，可预期）。

## 两条边界
· `filled` **由页面传** —— 服务端不许猜用户在页面上填了什么
· 每一项都要带 **为什么现在问它**（`why`）—— 让人不用猜对话的次序
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webapp                                                # noqa: E402
from intake import (ORIGIN_DERIVED, ORIGIN_EXTERNAL,        # noqa: E402
                    ORIGIN_FILING, ORIGIN_JUDGMENT, Q)


def _qs():
    return [
        Q("unit", "报表单位", origin=ORIGIN_FILING),
        Q("risk_free", "无风险利率", origin=ORIGIN_EXTERNAL, unit="%"),
        Q("growth", "营业收入增长率（逐年，逗号分隔）", origin=ORIGIN_JUDGMENT,
          unit="%"),
        Q("da_pct_revenue", "折旧摊销占收入比", origin=ORIGIN_DERIVED, unit="%",
          reference="8.84%"),
        Q("terminal_growth", "永续增长率", origin=ORIGIN_EXTERNAL, unit="%"),
        Q("capital_stock", "实收资本", origin=ORIGIN_FILING),
    ]


class TestAskOrder(unittest.TestCase):
    def test_derived_first_then_judgement_then_external_then_filing(self):
        order = [q.key for q, _ in webapp._ask_order(_qs(), set())]
        self.assertEqual(order[0], "da_pct_revenue", "先问零成本的那类")
        self.assertLess(order.index("da_pct_revenue"), order.index("growth"))
        self.assertLess(order.index("growth"), order.index("risk_free"))
        self.assertLess(order.index("risk_free"), order.index("unit"))

    def test_filled_items_are_skipped(self):
        order = [q.key for q, _ in webapp._ask_order(_qs(), {"da_pct_revenue"})]
        self.assertNotIn("da_pct_revenue", order)
        self.assertIn("growth", order)

    def test_every_item_says_why(self):
        for q, why in webapp._ask_order(_qs(), set()):
            self.assertTrue(why.strip(), f"{q.key} 没写为什么问它")
        reasons = {q.key: why for q, why in webapp._ask_order(_qs(), set())}
        self.assertIn("参考值", reasons["da_pct_revenue"])
        self.assertIn("公司之外", reasons["risk_free"])

    def test_judgement_items_carry_an_impact_reason(self):
        reasons = {q.key: why for q, why in webapp._ask_order(_qs(), set())}
        self.assertIn("DCF", reasons["growth"])

    def test_order_is_stable_within_a_bucket(self):
        """同一档内保持清单原顺序 —— 每次问的次序都一样，才可预期。"""
        a = [q.key for q, _ in webapp._ask_order(_qs(), set())]
        b = [q.key for q, _ in webapp._ask_order(_qs(), set())]
        self.assertEqual(a, b)

    def test_pickable_items_come_before_thinking_items(self):
        """★ 「点一下就好」的（有 options/suggest）必须排在"要动脑的数值项"前面。

        实测这一层原先只是**清单顺序的巧合**（估值目的那几项恰好在前）——
        所以这里**故意把下拉项放在后面**，看规则是不是真的显式成立。
        """
        qs = [
            Q("growth", "5 年收入增长率（逗号分隔）", origin=ORIGIN_JUDGMENT, unit="%"),
            Q("purpose", "估值目的", origin=ORIGIN_JUDGMENT,
              options=("交易定价", "投资决策")),
        ]
        order = [q.key for q, _ in webapp._ask_order(qs, set())]
        self.assertEqual(order, ["purpose", "growth"],
                         "下拉项没被提前 —— 规则还是靠清单顺序的巧合")

    def test_thinking_items_follow_the_impact_table(self):
        """数值项之间按影响表排：增长 > 折旧摊销 > 税率。"""
        qs = [
            Q("tax", "所得税率", origin=ORIGIN_JUDGMENT, unit="%"),
            Q("da", "折旧摊销占收入比", origin=ORIGIN_JUDGMENT, unit="%"),
            Q("growth", "5 年收入增长率", origin=ORIGIN_JUDGMENT, unit="%"),
        ]
        order = [q.key for q, _ in webapp._ask_order(qs, set())]
        self.assertEqual(order, ["growth", "da", "tax"])


class TestAskPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/fa-plan-test").as_posix()
        webapp._CACHE[self.tmp] = mock.Mock(directory=Path(self.tmp), is_file=False)
        self.p = mock.patch("intake.questions", return_value=_qs())
        self.p.start()

    def tearDown(self):
        self.p.stop()
        webapp._CACHE.pop(self.tmp, None)

    def test_plan_reports_counts_and_next(self):
        out = webapp.api_ask({"action": "plan", "path": self.tmp})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["total"], 6)
        self.assertEqual(out["filled"], 0)
        self.assertEqual(out["missing"], 6)
        self.assertEqual(out["next"]["key"], "da_pct_revenue")
        self.assertEqual(out["next"]["reference"], "8.84%",
                         "零成本那类必须带上参考值，界面才好给「沿用」")

    def test_plan_counts_what_the_page_says_is_filled(self):
        out = webapp.api_ask({"action": "plan", "path": self.tmp,
                              "filled": {"da_pct_revenue": "8.84%",
                                         "growth": "0.05"}})
        self.assertEqual(out["filled"], 2)
        self.assertEqual(out["missing"], 4)
        # 推算档 + 判断档都填完了 → 下一档是**外部**（要用户去查资料的那种）
        self.assertEqual(out["next"]["origin"], ORIGIN_EXTERNAL)

    def test_plan_without_material_is_honest(self):
        out = webapp.api_ask({"action": "plan", "path": "/nowhere"})
        self.assertFalse(out["ok"])
        self.assertIn("还没载入", out["error"])

    def test_all_filled_means_no_next(self):
        filled = {q.key: "x" for q in _qs()}
        out = webapp.api_ask({"action": "plan", "path": self.tmp, "filled": filled})
        self.assertEqual(out["missing"], 0)
        self.assertIsNone(out["next"])


if __name__ == "__main__":
    unittest.main()
