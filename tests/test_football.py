"""Football Field 的测试。

重点不是「画得好不好看」，是**几条纪律有没有守住**：
区间取网格极值、跨度报警、缺数据不许糊、不是估值的方法不进图。
"""

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valuation import football as fb  # noqa: E402


@dataclass
class _Result:
    """假的 ValuationResult —— 只带 Football Field 用得上的字段。"""
    method: str = "DCF"
    low: float = 0.0
    mid: float = 0.0
    high: float = 0.0
    unit: str = "元"
    is_valuation: bool = True


class TestBand(unittest.TestCase):
    def test_result_to_per_share(self):
        """股权价值 ÷ 股数 = 每股。"""
        b = fb.from_result("DCF", _Result("DCF", 8_000_000_000,
                                          10_000_000_000, 12_000_000_000),
                           shares=80_000_000)
        self.assertAlmostEqual(b.low, 100.0)
        self.assertAlmostEqual(b.mid, 125.0)
        self.assertAlmostEqual(b.high, 150.0)
        self.assertTrue(b.usable)

    def test_no_shares_refuses_to_draw(self):
        """**没有股数就不画。**

        拿「股权价值 100 亿」当成每股 100 亿画到图上，
        会让整张图的坐标轴失去意义。宁可留一条「算不出」。
        """
        b = fb.from_result("DCF", _Result("DCF", 8e9, 1e10, 1.2e10), shares=None)
        self.assertFalse(b.usable)
        self.assertIn("没有总股本", b.note)

    def test_non_valuation_excluded(self):
        """反向 VC 法不进图 —— 它算的不是「值多少」。"""
        b = fb.from_result("反向 VC", _Result("反向 VC", 1, 2, 3,
                                             is_valuation=False),
                           shares=80_000_000)
        self.assertFalse(b.usable)
        self.assertIn("不是估值", b.note)

    def test_span_computed(self):
        b = fb.Band("DCF", low=100.0, mid=125.0, high=150.0)
        self.assertAlmostEqual(b.span, 0.4)


class TestFootballField(unittest.TestCase):
    def _ff(self):
        bands = [
            fb.Band("DCF", low=100.0, mid=125.0, high=150.0),
            fb.Band("可比公司", low=110.0, mid=130.0, high=145.0),
            fb.Band("净资产", low=90.0, mid=105.0, high=120.0),
        ]
        return fb.build("某标的", bands, shares=80_000_000,
                        reference=120.0, reference_label="当前股价",
                        as_of="2025-12-31")

    def test_consensus_is_intersection(self):
        """共识区间是**交集**，不是平均。

        max(100, 110, 90) = 110；min(150, 145, 120) = 120 → 110–120。
        """
        c = self._ff().consensus()
        self.assertAlmostEqual(c[0], 110.0)
        self.assertAlmostEqual(c[1], 120.0)

    def test_no_intersection_is_reported_not_averaged(self):
        """**各方法没有交集时不许取平均糊过去。**

        分歧过大本身就是结论 —— 说明这个标的的估值高度依赖方法选择。
        """
        bands = [fb.Band("A", low=10.0, mid=20.0, high=30.0),
                 fb.Band("B", low=100.0, mid=120.0, high=140.0)]
        ff = fb.build("x", bands)
        self.assertIsNone(ff.consensus())
        self.assertIn("没有交集", ff.render_text())

    def test_wide_span_is_flagged(self):
        """跨度超中值 50% 要报警。"""
        bands = [fb.Band("DCF", low=50.0, mid=100.0, high=200.0)]   # 150%
        ff = fb.build("x", bands)
        self.assertTrue(any("跨度" in n and "150%" in n for n in ff.notes), ff.notes)

    def test_reference_comparison(self):
        """要能报出「哪些方法全部高于/低于基准价」。

        上面三条区间是 100–150 / 110–145 / 90–120，基准价 120 ——
        没有任何一条**完全**在基准价之上或之下（都跨过了 120），所以不该报。
        """
        ff = self._ff()
        self.assertFalse(any("全部低于" in n or "全部高于" in n for n in ff.notes),
                         ff.notes)
        # 换成明显低于基准价的一条，就该报了
        ff2 = fb.build("x", [fb.Band("净资产", 10.0, 20.0, 30.0)], reference=120.0)
        self.assertTrue(any("全部低于基准价" in n for n in ff2.notes), ff2.notes)

    def test_unusable_band_still_listed(self):
        """算不出的方法要**留在图上并说明原因**，不能悄悄消失。"""
        bands = [fb.Band("DCF", low=100.0, mid=125.0, high=150.0),
                 fb.Band("可比交易", note="没有可比交易数据")]
        ff = fb.build("x", bands)
        self.assertEqual(len(ff.bands), 2)
        self.assertEqual(len(ff.usable_bands()), 1)
        self.assertIn("可比交易", ff.render_text())


class TestRender(unittest.TestCase):
    def _ff(self):
        return fb.build("某标的",
                        [fb.Band("DCF", 100.0, 125.0, 150.0, confidence="中"),
                         fb.Band("可比公司", 110.0, 130.0, 145.0),
                         fb.Band("净资产", note="报表未披露")],
                        reference=120.0, reference_label="当前股价")

    def test_html_has_bars_and_reference_line(self):
        doc = fb.render_html(self._ff())
        self.assertIn("class=\"bar\"", doc)
        self.assertIn("class=\"ref\"", doc)
        self.assertIn("当前股价", doc)
        self.assertIn("共识区间", doc)

    def test_html_escapes(self):
        ff = fb.build("<script>x</script>", [])
        self.assertNotIn("<script>x</script>", fb.render_html(ff))

    def test_html_written_to_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ff.html"
            fb.render_html(self._ff(), p)
            self.assertTrue(p.exists())
            self.assertIn("<html", p.read_text(encoding="utf-8"))

    def test_json_roundtrip_for_other_renderers(self):
        """数据要能存 JSON —— 换 Excel 渲染时读它，不用重算。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ff.json"
            fb.save_json(self._ff(), p)
            d2 = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(len(d2["bands"]), 3)
            self.assertEqual(d2["reference"], 120.0)
            self.assertIn("span", d2["bands"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
