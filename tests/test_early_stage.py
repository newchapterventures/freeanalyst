"""早期项目估值测试。

## 这个文件锁住的几条

**① 手算的答案。** 每个方法的用例都先在注释里写出手算过程和期望值 ——
不是"跑通了"，是"算对了"。

**② 稀释会让估值变低，不是变高。** 这条反直觉，写错了方向不会报错。

    投资者要 20% 的退出股权。若有 25% 未来稀释，今天必须占到 26.67%。
    为同样的钱拿到更多股权 → 投后估值更低 → 投前估值更低。

**③ 概率合计不为 1 时必须报错，不能自动归一化。**
    加起来不是 1，通常说明漏了一个情景 —— 归一化会把"漏了什么"这个信息抹掉。

**④ 反向法必须能走回来。** 正向算出 pre-money，喂回反向法，
    应该还原出原来的退出价值。这是最硬的不变量。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valuation import early_stage as es  # noqa: E402
from valuation.core import Assumption, Confidence  # noqa: E402


A = Assumption


def _med(name, value, unit=""):
    return A(name, value, unit, "测试来源", Confidence.MEDIUM)


def _neutral_factors(mult=1.0):
    """Scorecard 的全中性因素集（全部与市场持平）。

    **必须是 Factor 而不是裸 Range** —— scorecard() 现在拒绝裸 Range，
    因为它会丢掉评分的出处。每个评分都要能回答"这分谁给的、凭什么"。
    """
    return {k: es.Factor(k, es.Range.point(mult), source="测试来源")
            for k in es.SCORECARD_WEIGHTS}


class TestBerkus(unittest.TestCase):
    """手算：0.6 × 5 个因素 × 封顶 50 = 150"""

    def _factors(self, score=0.6):
        return [es.Factor(n, es.Range.point(score), source="访谈",
                          confidence=Confidence.MEDIUM)
                for n in es.BERKUS_FACTORS]

    def test_hand_computed_point(self):
        r = es.berkus(self._factors(0.6), es.Range.point(50),
                      cap_source="用户给的锚点")
        self.assertAlmostEqual(r.mid, 150.0)
        self.assertAlmostEqual(r.low, 150.0)
        self.assertAlmostEqual(r.high, 150.0)

    def test_zero_progress_is_zero(self):
        r = es.berkus(self._factors(0.0), es.Range.point(50), cap_source="x")
        self.assertAlmostEqual(r.mid, 0.0)

    def test_full_progress_is_five_times_cap(self):
        r = es.berkus(self._factors(1.0), es.Range.point(50), cap_source="x")
        self.assertAlmostEqual(r.mid, 250.0)

    def test_factor_ranges_produce_a_result_range(self):
        """因素本身带不确定性时，结果就该是区间，不是单点。"""
        fs = [
            es.Factor(es.BERKUS_FACTORS[0], es.Range(0.4, 0.5, 0.6)),
            es.Factor(es.BERKUS_FACTORS[1], es.Range(0.2, 0.3, 0.4)),
        ]
        r = es.berkus(fs, es.Range.point(100.0), cap_source="x")
        self.assertAlmostEqual(r.low, (0.4 + 0.2) * 100)
        self.assertAlmostEqual(r.mid, (0.5 + 0.3) * 100)
        self.assertAlmostEqual(r.high, (0.6 + 0.4) * 100)

    def test_cap_range_widens_the_result(self):
        fs = self._factors(1.0)
        r = es.berkus(fs, es.Range(40, 50, 60), cap_source="x")
        self.assertAlmostEqual(r.low, 200.0)
        self.assertAlmostEqual(r.high, 300.0)

    def test_rejects_score_above_one(self):
        fs = [es.Factor("技术风险（做出原型）", es.Range.point(1.5))]
        with self.assertRaises(es.EarlyStageError) as cm:
            es.berkus(fs, es.Range.point(50), cap_source="x")
        self.assertIn("0–1", str(cm.exception))

    def test_rejects_nonpositive_cap(self):
        with self.assertRaises(es.EarlyStageError):
            es.berkus(self._factors(), es.Range.point(0), cap_source="x")

    def test_rejects_empty_factors(self):
        with self.assertRaises(es.EarlyStageError):
            es.berkus([], es.Range.point(50), cap_source="x")

    def test_cap_without_source_is_flagged(self):
        """锚点没来源必须报出来 —— 它是结果里权重最大的一个数。"""
        r = es.berkus(self._factors(), es.Range.point(50),
                      cap_source="未注明", cap_confidence=Confidence.MISSING)
        self.assertTrue(any("没有来源" in n for n in r.notes))
        self.assertIn(r.assumptions[0], r.gaps() or [r.assumptions[0]])

    def test_missing_standard_factors_are_listed(self):
        fs = [es.Factor(es.BERKUS_FACTORS[0], es.Range.point(0.5))]
        r = es.berkus(fs, es.Range.point(50), cap_source="x")
        self.assertTrue(any("未评估的风险因素" in n for n in r.notes))

    def test_revenue_component_is_labeled_as_an_extension(self):
        r = es.berkus(self._factors(0.5), es.Range.point(50), cap_source="x",
                      revenue_component=A("收入加项", 30.0, "万元", "已签合同",
                                          Confidence.HIGH))
        self.assertAlmostEqual(r.mid, 0.5 * 5 * 50 + 30)
        self.assertTrue(any("原版 Berkus 之外的扩展" in n for n in r.notes))


class TestScorecard(unittest.TestCase):
    """手算：基准 1000，全部倍率 1.0 → 1000"""

    def test_all_neutral_equals_base(self):
        """这是这个方法的定义性质：跟市场平均一样，就值市场平均。"""
        sb = _neutral_factors(1.0)
        r = es.scorecard(es.Range.point(1000), "某机构报告", sb)
        self.assertAlmostEqual(r.mid, 1000.0)
        self.assertIn("持平", r.notes[-1])

    def test_uniform_premium_scales_linearly(self):
        """全部 1.2 倍 → 基准 × 1.2"""
        sb = _neutral_factors(1.2)
        r = es.scorecard(es.Range.point(1000), "x", sb)
        self.assertAlmostEqual(r.mid, 1200.0)

    def test_weighted_adjustment_hand_computed(self):
        """手算：团队 1.5(权重 0.30) + 其余 1.0
        Σ(w×m) = 0.30×1.5 + 0.70×1.0 = 0.45 + 0.70 = 1.15
        1000 × 1.15 = 1150"""
        sb = _neutral_factors(1.0)
        sb["管理团队实力"] = es.Factor("管理团队实力", es.Range.point(1.5))
        r = es.scorecard(es.Range.point(1000), "x", sb)
        self.assertAlmostEqual(r.mid, 1150.0)

    def test_negative_adjustment(self):
        """手算：竞争环境 0.5(权重 0.10) → 1 − 0.10×0.5 = 0.95 → 950"""
        sb = _neutral_factors(1.0)
        sb["竞争环境"] = es.Factor("竞争环境", es.Range.point(0.5))
        r = es.scorecard(es.Range.point(1000), "x", sb)
        self.assertAlmostEqual(r.mid, 950.0)

    def test_base_range_produces_result_range(self):
        sb = _neutral_factors(1.0)
        r = es.scorecard(es.Range(800, 1000, 1200), "x", sb)
        self.assertAlmostEqual(r.low, 800.0)
        self.assertAlmostEqual(r.high, 1200.0)

    def test_rejects_out_of_range_multiplier(self):
        sb = _neutral_factors(1.0)
        sb["机会规模"] = es.Factor("机会规模", es.Range.point(2.0))
        with self.assertRaises(es.EarlyStageError) as cm:
            es.scorecard(es.Range.point(1000), "x", sb)
        self.assertIn("0.5", str(cm.exception))

    def test_unknown_factor_name_raises(self):
        with self.assertRaises(es.EarlyStageError) as cm:
            es.scorecard(es.Range.point(1000), "x", {"团队好不好": es.Factor("团队好不好", es.Range.point(1.0))})
        self.assertIn("不认识的评分因素", str(cm.exception))

    def test_partial_factors_are_renormalized_and_noted(self):
        """只给部分因素时按比例归一化，**并且要说明**。"""
        r = es.scorecard(es.Range.point(1000), "x", {"管理团队实力": es.Factor("管理团队实力", es.Range.point(1.5))})
        self.assertAlmostEqual(r.mid, 1500.0)   # 归一化后权重变成 1.0
        self.assertTrue(any("归一化" in n for n in r.notes))

    def test_bad_weight_sum_raises_for_list_input(self):
        fs = [es.Factor("A", es.Range.point(1.0), weight=0.5),
              es.Factor("B", es.Range.point(1.0), weight=0.3)]
        with self.assertRaises(es.EarlyStageError) as cm:
            es.scorecard(es.Range.point(1000), "x", fs)
        self.assertIn("权重合计", str(cm.exception))

    def test_rejects_bare_range_in_dict(self):
        """传裸 Range 会丢掉评分的出处，必须拒绝并说清原因。"""
        with self.assertRaises(es.EarlyStageError) as cm:
            es.scorecard(es.Range.point(1000), "x",
                         {"管理团队实力": es.Range.point(1.2)})
        self.assertIn("Factor", str(cm.exception))
        self.assertIn("出处", str(cm.exception))

    def test_factor_source_is_carried_through(self):
        """因素的来源必须传到结果里 —— 不能被覆盖成"用户输入"。"""
        sb = _neutral_factors()
        sb["管理团队实力"] = es.Factor("管理团队实力", es.Range.point(1.3),
                                       source="与创始人两轮访谈",
                                       confidence=Confidence.MEDIUM)
        r = es.scorecard(es.Range.point(1000), "x", sb)
        got = [a for a in r.assumptions if a.name == "管理团队实力"][0]
        self.assertEqual(got.source, "与创始人两轮访谈")
        self.assertIs(got.confidence, Confidence.MEDIUM)

    def test_point_base_is_flagged(self):
        r = es.scorecard(es.Range.point(1000), "x", _neutral_factors())
        self.assertTrue(any("单点" in n for n in r.notes))


class TestVcMethod(unittest.TestCase):
    """手算：退出 50000，投 1000，目标 10x
    own_exit = 10×1000/50000 = 0.20
    post = 1000/0.20 = 5000；pre = 4000"""

    def _base_args(self):
        return dict(
            exit_value=_med("退出价值", 50000),
            investment=_med("投资额", 1000),
            years_to_exit=_med("退出年限", 5),
        )

    def test_hand_computed(self):
        r = es.vc_method(target_multiple=_med("目标倍数", 10.0), **self._base_args())
        self.assertAlmostEqual(r.mid, 4000.0)

    def test_dilution_lowers_the_valuation(self):
        """**稀释让投前估值变低，不是变高。** 反直觉，写反了不会报错。

        手算：own_exit=0.20；own_today = 0.20/0.75 = 0.26667
              post = 1000/0.26667 = 3750；pre = 2750
        """
        r0 = es.vc_method(target_multiple=_med("目标倍数", 10.0), **self._base_args())
        r1 = es.vc_method(target_multiple=_med("目标倍数", 10.0),
                          future_dilution=_med("未来稀释", 0.25), **self._base_args())
        self.assertAlmostEqual(r1.mid, 2750.0)
        self.assertLess(r1.mid, r0.mid,
                        "有稀释时投前必须更低：同样的钱要换更多股权")

    def test_higher_target_multiple_lowers_the_valuation(self):
        """目标回报要求越高，今天能付的越少。"""
        lo = es.vc_method(target_multiple=_med("目标倍数", 5.0), **self._base_args())
        hi = es.vc_method(target_multiple=_med("目标倍数", 10.0), **self._base_args())
        self.assertGreater(lo.mid, hi.mid)

    def test_irr_and_multiple_are_cross_checked(self):
        """10 年 10x 对应的 IRR 是 25.9%，给 15% 就该报不一致。"""
        r = es.vc_method(target_multiple=_med("目标倍数", 10.0),
                         target_irr=_med("目标 IRR", 0.15),
                         **self._base_args())
        self.assertTrue(any("不一致" in n for n in r.notes))

    def test_consistent_irr_raises_no_warning(self):
        """5 年 10x ⟹ IRR = 10^(1/5) − 1 = 58.49%"""
        r = es.vc_method(target_multiple=_med("目标倍数", 10.0),
                         target_irr=_med("目标 IRR", 10 ** 0.2 - 1),
                         **self._base_args())
        self.assertFalse(any("不一致" in n for n in r.notes))

    def test_irr_alone_works(self):
        """只给 IRR 时自动折算倍数：(1+0.5)^5 = 7.59x
        own_exit = 7.59375×1000/50000 = 0.151875
        post = 1000/0.151875 = 6584.4；pre = 5584.4"""
        r = es.vc_method(target_irr=_med("目标 IRR", 0.5), **self._base_args())
        self.assertAlmostEqual(r.mid, 1000 / (7.59375 * 1000 / 50000) - 1000, places=4)

    def test_requires_a_target(self):
        with self.assertRaises(es.EarlyStageError):
            es.vc_method(**self._base_args())

    def test_rejects_missing_inputs(self):
        with self.assertRaises(es.EarlyStageError):
            es.vc_method(
                exit_value=A("退出价值", None, confidence=Confidence.MISSING),
                investment=_med("投资额", 1000),
                years_to_exit=_med("退出年限", 5),
                target_multiple=_med("目标倍数", 10.0),
            )

    def test_rejects_target_multiple_at_or_below_one(self):
        with self.assertRaises(es.EarlyStageError):
            es.vc_method(target_multiple=_med("目标倍数", 1.0), **self._base_args())

    def test_no_dilution_input_is_flagged_as_optimistic(self):
        r = es.vc_method(target_multiple=_med("目标倍数", 10.0), **self._base_args())
        self.assertTrue(any("乐观假设" in n for n in r.notes))

    def test_impossible_target_is_noted(self):
        """退出价太小、目标倍数太高 → 需要超过 100% 的股权。"""
        r = es.vc_method(
            exit_value=_med("退出价值", 5000),
            investment=_med("投资额", 1000),
            years_to_exit=_med("退出年限", 5),
            target_multiple=_med("目标倍数", 10.0),
        )
        self.assertTrue(any("超过 100%" in n for n in r.notes))


class TestReverseVc(unittest.TestCase):
    """反向法是 §6.6 的强制要求：有报价就要跑。"""

    def test_round_trip_with_forward_method(self):
        """**最硬的不变量**：正向算出的 pre，喂回反向法应还原退出价值。"""
        args = dict(
            exit_value=_med("退出价值", 50000),
            investment=_med("投资额", 1000),
            years_to_exit=_med("退出年限", 5),
        )
        fwd = es.vc_method(target_multiple=_med("目标倍数", 10.0), **args)
        rev = es.vc_required_exit(
            pre_money=_med("投前", fwd.mid),
            investment=_med("投资额", 1000),
            years_to_exit=_med("退出年限", 5),
            target_multiple=_med("目标倍数", 10.0),
        )
        self.assertAlmostEqual(rev.mid, 50000.0, places=6)

    def test_round_trip_with_dilution(self):
        args = dict(
            exit_value=_med("退出价值", 50000),
            investment=_med("投资额", 1000),
            years_to_exit=_med("退出年限", 5),
        )
        fwd = es.vc_method(target_multiple=_med("目标倍数", 10.0),
                           future_dilution=_med("未来稀释", 0.25), **args)
        rev = es.vc_required_exit(
            pre_money=_med("投前", fwd.mid),
            investment=_med("投资额", 1000),
            years_to_exit=_med("退出年限", 5),
            target_multiple=_med("目标倍数", 10.0),
            future_dilution=_med("未来稀释", 0.25),
        )
        self.assertAlmostEqual(rev.mid, 50000.0, places=6)

    def test_higher_price_requires_bigger_exit(self):
        """报价越高，需要的退出价值越大。"""
        common = dict(investment=_med("投资额", 1000),
                      years_to_exit=_med("退出年限", 5),
                      target_multiple=_med("目标倍数", 10.0))
        cheap = es.vc_required_exit(pre_money=_med("投前", 4000), **common)
        dear = es.vc_required_exit(pre_money=_med("投前", 20000), **common)
        self.assertGreater(dear.mid, cheap.mid)

    def test_reports_implied_cagr(self):
        """输出的重点不是"贵不贵"，是"要涨多少"。"""
        r = es.vc_required_exit(
            pre_money=_med("投前", 4000), investment=_med("投资额", 1000),
            years_to_exit=_med("退出年限", 5), target_multiple=_med("目标倍数", 10.0),
        )
        self.assertTrue(any("年化" in n for n in r.notes))
        self.assertTrue(any("年化" in label or "年化" in expr for label, expr in r.trace.steps))


class TestFirstChicago(unittest.TestCase):
    def test_hand_computed_weighted_value(self):
        """手算：0.1×50000 + 0.5×8000 + 0.4×0 = 5000 + 4000 = 9000"""
        r = es.first_chicago([
            es.Outcome("成功", 0.1, 50000),
            es.Outcome("存活", 0.5, 8000),
            es.Outcome("失败", 0.4, 0),
        ])
        self.assertAlmostEqual(r.mid, 9000.0)
        self.assertAlmostEqual(r.low, 0.0)
        self.assertAlmostEqual(r.high, 50000.0)

    def test_probabilities_must_sum_to_one(self):
        """**不能自动归一化** —— 加起来不是 1 通常说明漏了一个情景。"""
        with self.assertRaises(es.EarlyStageError) as cm:
            es.first_chicago([
                es.Outcome("成功", 0.1, 50000),
                es.Outcome("失败", 0.3, 0),
            ])
        msg = str(cm.exception)
        self.assertIn("1.0", msg)
        self.assertIn("漏了一个情景", msg)

    def test_zero_scenario_is_called_out(self):
        """pre-revenue 的分布是一头沉的，含归零情景时必须说明。"""
        r = es.first_chicago([es.Outcome("成功", 0.2, 50000),
                              es.Outcome("失败", 0.8, 0)])
        self.assertTrue(any("一头沉" in n for n in r.notes))

    def test_success_case_reported_separately_from_weighted(self):
        r = es.first_chicago([es.Outcome("成功", 0.2, 50000),
                              es.Outcome("失败", 0.8, 0)])
        self.assertTrue(any("两个数都要看" in n for n in r.notes))

    def test_rejects_negative_value(self):
        with self.assertRaises(es.EarlyStageError):
            es.first_chicago([es.Outcome("成功", 1.0, -5)])

    def test_rejects_empty(self):
        with self.assertRaises(es.EarlyStageError):
            es.first_chicago([])


class TestCompare(unittest.TestCase):
    """§6.7：多方法之间的差额必须报出来。"""

    def _res(self, method, mid, is_valuation=True):
        from valuation.core import Trace, ValuationResult
        return ValuationResult(method, mid, mid, mid, "万元", Trace(),
                               is_valuation=is_valuation)

    def test_reports_spread(self):
        out = es.compare([self._res("Berkus 法（5 个风险因素）", 150),
                          self._res("VC 法（目标 10.0x / 5 年）", 4000)])
        self.assertIn("中枢差额", out)
        self.assertIn("超过 50%", out)

    def test_reverse_result_is_excluded_from_the_spread(self):
        """**反向估值不参与差额。**

        它输出的是「需要多大的退出」，不是「值多少」。
        混进来算差额会得出一个毫无意义的数，而且看起来还挺像回事 ——
        实测时它把差额从合理的量级拉到了 199%。
        """
        out = es.compare([
            self._res("Berkus 法", 870),
            self._res("VC 法", 6400),
            self._res("反向 VC 法", 242857, is_valuation=False),
        ])
        spread_line = [l for l in out.splitlines() if "中枢差额" in l][0]
        self.assertNotIn("242,857", spread_line)
        self.assertIn("不属于估值", out)
        self.assertIn("需要多大的退出", out)

    def test_only_reverse_results_gives_no_spread(self):
        out = es.compare([self._res("反向 VC 法", 242857, is_valuation=False)])
        self.assertNotIn("中枢差额", out)
        self.assertIn("不属于估值", out)

    def test_explains_why_methods_differ(self):
        out = es.compare([self._res("A", 100), self._res("B", 4000)])
        self.assertIn("三个问题不同", out)

    def test_small_spread_no_warning(self):
        out = es.compare([self._res("A", 1000), self._res("B", 1050)])
        self.assertNotIn("超过 50%", out)

    def test_empty(self):
        self.assertIn("没有可对照", es.compare([]))


class TestDisplayFormatting(unittest.TestCase):
    """追溯里的数字必须和输入对得上。

    这里锁的是一个真实踩过的 bug：`Range.__str__` 原来用固定的 `:,.0f`，
    把达成度 0.7 / 0.8 / 0.9 全显示成 "1"。公式没错，但**追溯里的数
    和输入对不上** —— 这比算错更糟，因为人没法核对了。
    """

    def test_small_decimals_are_preserved(self):
        r = es.Range(0.7, 0.8, 0.9)
        self.assertEqual(str(r), "0.7 / 0.8 / 0.9")

    def test_point_decimals(self):
        self.assertEqual(str(es.Range.point(0.65)), "0.65")

    def test_large_numbers_get_thousands_separator(self):
        self.assertEqual(str(es.Range.point(120000)), "120,000")

    def test_zero(self):
        self.assertEqual(str(es.Range.point(0)), "0")

    def test_mid_range_values(self):
        """300、1.5 这类中间量级也要正常。"""
        self.assertEqual(str(es.Range.point(300)), "300")
        self.assertEqual(str(es.Range.point(1.5)), "1.5")

    def test_trace_shows_factor_values_not_rounded(self):
        """端到端：Berkus 的追溯里必须出现 0.8，不能是 1。"""
        fs = [es.Factor(es.BERKUS_FACTORS[0], es.Range.point(0.8))]
        r = es.berkus(fs, es.Range.point(300), cap_source="x")
        text = r.trace.render()
        self.assertIn("0.8", text)
        self.assertIn("300", text)


class TestRangeValidation(unittest.TestCase):
    def test_rejects_inverted_range(self):
        with self.assertRaises(es.EarlyStageError):
            es.Range(10, 5, 20)

    def test_point_helper(self):
        self.assertTrue(es.Range.point(7).is_point)


if __name__ == "__main__":
    unittest.main()
