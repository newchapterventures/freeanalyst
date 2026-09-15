"""假设参谋测试。

## 这个文件锁住的三件事

**① 分布是真算出来的，不是编的。**
   每个用例手算答案写在注释里 —— 收入从 100 到 133.1 走三年，
   就是 10% 的 CAGR（1.1³ = 1.331）。

**② 前视偏差在同一套口径下不能出现。**
   comps 模块做了这个检查，advisor 也必须做 ——
   否则同一个项目里两套纪律。用错口径会让标的的假设
   *看起来比实际保守*，那是最容易骗过自己的方向。

**③ 拿不到分布就说拿不到，不凑参照系。**
   硬凑的参照系比没有更糟：它会让用户以为自己有了依据。
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valuation import advisor as ad  # noqa: E402
from valuation.core import Assumption, Confidence  # noqa: E402

A = Assumption


def _row(start, end, val, filed):
    return {"start": start, "end": end, "val": val, "form": "10-K",
            "fy": 2025, "fp": "FY", "filed": filed, "accn": f"a{filed}"}


def _facts(rows):
    return {"entityName": "Test Co", "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rows}},
    }}}


# 100 → 110 → 121 → 133.1 三年，CAGR 正好 10%
EXACT_10 = _facts([
    _row("2022-01-01", "2022-12-31", 100.0, "2023-02-01"),
    _row("2023-01-01", "2023-12-31", 110.0, "2024-02-01"),
    _row("2024-01-01", "2024-12-31", 121.0, "2025-02-01"),
    _row("2025-01-01", "2025-12-31", 133.1, "2026-02-01"),
])


class TestPeerStatQuantiles(unittest.TestCase):
    def _stat(self, values):
        return ad.PeerStat("x", values, ["a"] * len(values), "test")

    def test_quantiles(self):
        s = self._stat([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(s.quantile(0.25), 2.0)
        self.assertAlmostEqual(s.median(), 3.0)
        self.assertAlmostEqual(s.quantile(0.75), 4.0)

    def test_percentile_is_a_count_not_interpolation(self):
        """用户问的是「我比多少家高」，那是个计数问题。"""
        s = self._stat([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(s.percentile_of(2.5), 0.5)   # 比 2 家高
        self.assertAlmostEqual(s.percentile_of(0.5), 0.0)   # 比 0 家高
        self.assertAlmostEqual(s.percentile_of(9.0), 1.0)   # 比全部高

    def test_single_value(self):
        s = self._stat([7.0])
        self.assertAlmostEqual(s.median(), 7.0)
        self.assertAlmostEqual(s.percentile_of(7.0), 1.0)

    def test_empty(self):
        s = self._stat([])
        self.assertIsNone(s.median())
        self.assertIn("无可用样本", s.render())


class TestRevenueCagr(unittest.TestCase):
    def test_hand_computed_exact_10_percent(self):
        """100 → 133.1 走三年 ≈ 10% CAGR（1.1³ = 1.331）。

        **不完全等于 10%**，因为跨度按实际天数算：2022-12-31 到 2025-12-31
        是 1096 天，不是 1095.75 天（3 × 365.25）。所以是 9.9976%。

        这是**有意的**：财年长度会变（52/53 周制、财年变更），
        假设「N 个财年 = N.0 年」会在跨财年变更时算错。
        """
        v, why = ad.revenue_cagr(EXACT_10, years=3)
        self.assertAlmostEqual(v, 0.10, delta=0.001)
        self.assertLess(v, 0.10, "1096 天比 3.0 年略长，所以 CAGR 略低于 10%")
        self.assertIn("已披露" if "已披露" in why else "全部数据", why)

    def test_reports_the_actual_period(self):
        _, why = ad.revenue_cagr(EXACT_10, years=3)
        self.assertIn("2022-12-31", why)
        self.assertIn("2025-12-31", why)

    def test_not_enough_years(self):
        v, why = ad.revenue_cagr(EXACT_10, years=5)
        self.assertIsNone(v)
        self.assertIn("不足", why)

    def test_negative_growth(self):
        """收入腰斩也要能算，不能因为负数就返回 None。"""
        f = _facts([
            _row("2022-01-01", "2022-12-31", 200.0, "2023-02-01"),
            _row("2023-01-01", "2023-12-31", 150.0, "2024-02-01"),
            _row("2024-01-01", "2024-12-31", 100.0, "2025-02-01"),
            _row("2025-01-01", "2025-12-31", 100.0, "2026-02-01"),
        ])
        v, _ = ad.revenue_cagr(f, years=3)
        self.assertIsNotNone(v)
        self.assertLess(v, 0)

    def test_zero_base_returns_none(self):
        f = _facts([
            _row("2022-01-01", "2022-12-31", 0.0, "2023-02-01"),
            _row("2023-01-01", "2023-12-31", 100.0, "2024-02-01"),
        ])
        v, why = ad.revenue_cagr(f, years=1)
        self.assertIsNone(v)


class TestLookAheadPrevention(unittest.TestCase):
    """**和 comps 模块同一套口径。** 同一个项目不能有两套纪律。"""

    def test_as_of_excludes_later_filings(self):
        """FY2025 是 2026-02-01 才披露的。

        问 2025-06-30 的 CAGR，只能用到 FY2024 为止 ——
        用上 FY2025 就等于用了当时还不存在的数据。
        """
        v_all, why_all = ad.revenue_cagr(EXACT_10, years=2)
        v_as, why_as = ad.revenue_cagr(EXACT_10, years=2, as_of="2025-06-30")

        # 2023→2025 和 2022→2024 都是 100→121，比率一样，
        # 但跨度天数不同（730 vs 1096），所以都不是精确的 10%。
        # 用 delta 而不是 places：这里的偏差来自天数折算，量级是 1e-4 级，
        # 写 places 会随样本日期变化而莫名失败。
        self.assertAlmostEqual(v_all, 0.10, delta=0.001)
        self.assertAlmostEqual(v_as, 0.10, delta=0.001)
        # 关键：期末不同
        self.assertIn("2025-12-31", why_all)
        self.assertIn("2024-12-31", why_as)

    def test_as_of_withholds_when_not_enough_disclosed(self):
        """2023-01-01 时，连 FY2022 的年报都还没出（2023-02-01 才披露）。"""
        v, why = ad.revenue_cagr(EXACT_10, years=1, as_of="2023-01-01")
        self.assertIsNone(v)
        self.assertIn("已披露", why)

    def test_label_says_which_basis(self):
        _, why_d = ad.revenue_cagr(EXACT_10, years=2, as_of="2025-06-30")
        _, why_a = ad.revenue_cagr(EXACT_10, years=2)
        self.assertIn("已披露口径", why_d)
        self.assertIn("含基准日之后才披露的", why_a)


class TestClassify(unittest.TestCase):
    def test_known_kinds(self):
        self.assertEqual(ad.classify("2027 年收入增长率"), "revenue_growth")
        self.assertEqual(ad.classify("EBITDA 率"), "ebitda_margin")
        self.assertEqual(ad.classify("WACC"), "wacc")
        self.assertEqual(ad.classify("永续增长率"), "terminal_growth")

    def test_specific_kind_wins_over_generic(self):
        """**顺序依赖的 bug。**

        「永续增长率」既含「永续」也含「增长率」。如果 revenue_growth
        的关键词先被匹配，它会被错误归成收入增长假设 ——
        于是拿到一份完全不相干的证据清单。

        实测踩到过：这个断言当时返回的是 'revenue_growth'。
        """
        self.assertEqual(ad.classify("永续增长率"), "terminal_growth")
        self.assertEqual(ad.classify("终值增长率"), "terminal_growth")
        # 但普通的收入增长不能被误判成永续
        self.assertEqual(ad.classify("2027 年收入增长率"), "revenue_growth")

    def test_unknown(self):
        self.assertIsNone(ad.classify("办公室租金"))


class TestEvidenceLibrary(unittest.TestCase):
    """清单本身是知识，要能被审阅和修改。"""

    def test_all_kinds_have_entries(self):
        for kind in ("revenue_growth", "ebitda_margin", "wacc", "terminal_growth"):
            with self.subTest(kind=kind):
                self.assertTrue(ad.EVIDENCE_LIBRARY.get(kind), f"{kind} 的清单是空的")

    def test_every_item_has_level_what_where(self):
        for kind, items in ad.EVIDENCE_LIBRARY.items():
            for it in items:
                with self.subTest(kind=kind, what=it.what):
                    self.assertIn(it.level, ("A", "B", "C"))
                    self.assertTrue(it.what.strip())
                    self.assertTrue(it.where.strip())
                    self.assertTrue(it.why.strip(),
                                    "每条证据都要说清为什么它能支撑/推翻假设")

    def test_revenue_growth_covers_the_volume_price_split(self):
        """「量 × 价」是收入增长分析里最该被追问的一点。"""
        joined = " ".join(i.why for i in ad.EVIDENCE_LIBRARY["revenue_growth"])
        self.assertIn("放量", joined)
        self.assertIn("提价", joined)

    def test_margin_covers_one_off_addbacks(self):
        joined = " ".join(i.why for i in ad.EVIDENCE_LIBRARY["ebitda_margin"])
        self.assertIn("一次性", joined)


class TestTerminalGrowthCheck(unittest.TestCase):
    """**一家公司永远比整个经济长得快，在数学上不成立。**"""

    def setUp(self):
        self.gdp = A("长期名义GDP增速", 0.045, "", "社科院", Confidence.MEDIUM)
        self.infl = A("长期通胀预期", 0.02, "", "央行", Confidence.HIGH)

    def test_reasonable_passes(self):
        g = A("永续增长", 0.025, "", "用户", Confidence.LOW)
        self.assertEqual(ad.terminal_growth_check(g, self.gdp, self.infl), [])

    def test_above_gdp_is_rejected(self):
        g = A("永续增长", 0.06, "", "用户", Confidence.LOW)
        probs = ad.terminal_growth_check(g, self.gdp, self.infl)
        self.assertTrue(any("超过" in p and "数学上不成立" in p for p in probs))

    def test_near_the_ceiling_is_warned(self):
        g = A("永续增长", 0.043, "", "用户", Confidence.LOW)
        probs = ad.terminal_growth_check(g, self.gdp, self.infl)
        self.assertTrue(any("接近" in p for p in probs))
        self.assertFalse(any("数学上不成立" in p for p in probs))

    def test_below_inflation_is_warned(self):
        g = A("永续增长", 0.01, "", "用户", Confidence.LOW)
        probs = ad.terminal_growth_check(g, self.gdp, self.infl)
        self.assertTrue(any("低于长期通胀" in p for p in probs))

    def test_inflation_is_optional(self):
        g = A("永续增长", 0.01, "", "用户", Confidence.LOW)
        probs = ad.terminal_growth_check(g, self.gdp)
        self.assertFalse(any("通胀" in p for p in probs))

    def test_missing_inputs_say_so(self):
        g = A("永续增长", None, "", "未提供", Confidence.MISSING)
        probs = ad.terminal_growth_check(g, self.gdp, self.infl)
        self.assertTrue(any("缺失" in p for p in probs))


class TestAdvise(unittest.TestCase):
    def _stat(self):
        return ad.PeerStat("近三年收入 CAGR", [0.02, 0.05, 0.08, 0.11],
                           ["a", "b", "c", "d"], "EDGAR")

    def test_high_percentile_is_flagged(self):
        a = A("2027 年收入增长", 0.146, "", "管理层规划", Confidence.LOW)
        adv = ad.advise(a, peer_stat=self._stat())
        self.assertAlmostEqual(adv.percentile, 1.0)
        self.assertIn("高于 P75", adv.render())

    def test_low_percentile_is_flagged(self):
        a = A("2027 年收入增长", 0.01, "", "管理层规划", Confidence.LOW)
        adv = ad.advise(a, peer_stat=self._stat())
        self.assertAlmostEqual(adv.percentile, 0.0)
        self.assertIn("低于 P25", adv.render())

    def test_no_peer_data_does_not_fabricate_a_benchmark(self):
        """**硬凑参照系比没有更糟：它会让用户以为自己有了依据。**"""
        a = A("2027 年收入增长", 0.10, "", "管理层规划", Confidence.LOW)
        adv = ad.advise(a)
        self.assertIsNone(adv.peer)
        out = adv.render()
        self.assertIn("不给你凑一个参照系", out)

    def test_evidence_list_attached_for_known_kind(self):
        a = A("2027 年收入增长", 0.10, "", "管理层规划", Confidence.LOW)
        adv = ad.advise(a, peer_stat=self._stat())
        self.assertEqual(adv.kind, "revenue_growth")
        self.assertTrue(len(adv.evidence) >= 5)

    def test_unknown_kind_still_works_and_says_so(self):
        a = A("办公室租金涨幅", 0.05, "", "租约", Confidence.HIGH)
        adv = ad.advise(a, peer_stat=self._stat())
        self.assertIsNone(adv.kind)
        self.assertEqual(adv.evidence, [])
        self.assertIn("还没覆盖到", adv.render())

    def test_confidence_is_called_out(self):
        a = A("2027 年收入增长", 0.10, "", "管理层规划 p.12", Confidence.LOW)
        out = ad.advise(a, peer_stat=self._stat()).render()
        self.assertIn("C 级", out)
        self.assertIn("结论强度不能超过这个假设本身的强度", out)

    def test_high_confidence_is_acknowledged(self):
        a = A("2027 年收入增长", 0.10, "", "审计报告", Confidence.HIGH)
        out = ad.advise(a, peer_stat=self._stat()).render()
        self.assertIn("A 级材料", out)

    def test_missing_value_goes_to_gaps(self):
        a = A("2027 年收入增长", None, "", "未提供", Confidence.MISSING)
        adv = ad.advise(a, peer_stat=self._stat())
        self.assertTrue(any("没有值" in g for g in adv.gaps))

    def test_peer_gaps_are_shown(self):
        stat = ad.PeerStat("x", [0.05], ["a"], "EDGAR",
                           gaps=["b：不足 4 个年度值", "c：取数失败"])
        a = A("2027 年收入增长", 0.10, "", "x", Confidence.LOW)
        out = ad.advise(a, peer_stat=stat).render()
        self.assertIn("未能计入样本", out)
        self.assertIn("b：不足", out)

    def test_render_is_line_based_not_char_by_char(self):
        """实测踩过：多行文本被 for 循环逐字吐出来。"""
        a = A("2027 年收入增长", 0.10, "", "x", Confidence.LOW)
        out = ad.advise(a, peer_stat=self._stat()).render()
        self.assertIn("近三年收入 CAGR", out)
        # 不能再出现「近\n三\n年」这种
        self.assertNotIn("近\n", out)


class TestMetricErrors(unittest.TestCase):
    def test_unknown_metric_raises_with_options(self):
        with self.assertRaises(ValueError) as cm:
            ad.build_peer_stat([], metric="不存在的指标")
        self.assertIn("revenue_cagr", str(cm.exception))

    def test_empty_peer_list_gives_empty_stat_not_crash(self):
        stat = ad.build_peer_stat([], metric="revenue_cagr")
        self.assertEqual(stat.n, 0)
        self.assertIsNone(stat.median())


if __name__ == "__main__":
    unittest.main()
