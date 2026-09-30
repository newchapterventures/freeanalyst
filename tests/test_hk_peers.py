"""港股同行分布：口径与"不硬凑"的纪律（假数据，不联网）。

★ 这里要钉住的几件事，都是**错了从数字上看不出来**的那一类：
  1. 源给的比率是**百分数**（29.25 表示 29.25%），进分布前必须变成小数，
     否则格式串 `%` 会把它放大 100 倍；
  2. `revenue_scale` / `pe` / `pb` **不是比率** —— 它们带币种或价格口径，
     不能被同一条除 100 的规则误伤；
  3. 一家缺科目就记**缺口**，绝不拿 0 填（与 A 股 / 美股两条路同纪律）。
"""
from __future__ import annotations

import unittest

from datasources import hk_financials as hf
from valuation import advisor as ad


def _fake_row(name: str, **fields):
    return {"code": "00000", "name": name, "period": "2025-12-31",
            "period_label": "2025年年报", "currency": "HKD",
            "source": "测试固定件", "fields": fields}


class HkPeersTest(unittest.TestCase):

    def setUp(self) -> None:
        self._orig = hf.fetch

    def tearDown(self) -> None:
        hf.fetch = self._orig

    def _feed(self, table: dict):
        def fake(code: str):
            return table.get(code)
        hf.fetch = fake

    def test_op_margin_is_computed_from_the_same_row(self):
        self._feed({"00001": _fake_row("甲公司", 营业收入=100.0, 营业利润=20.0)})
        st = ad.build_hk_peer_stat(["00001"], metric="op_margin")
        self.assertEqual(st.n, 1)
        self.assertAlmostEqual(st.values[0], 0.20, places=6)
        self.assertEqual(st.unit, ".1%")

    def test_source_ratios_are_divided_by_100(self):
        """源给 29.25 表示 29.25% —— 存小数，别让格式串再放大一百倍。"""
        self._feed({"00001": _fake_row("甲公司", 净利率=29.25)})
        st = ad.build_hk_peer_stat(["00001"], metric="net_margin")
        self.assertAlmostEqual(st.values[0], 0.2925, places=6)

    def test_multiples_and_scale_are_NOT_rescaled(self):
        """pe / pb / 规模不是比率 —— 不能被"除 100"这条规则误伤。"""
        self._feed({"00001": _fake_row("甲公司", 市盈率TTM=14.72,
                                       市净率TTM=3.004, 营业收入=401_243_000_000.0)})
        pe = ad.build_hk_peer_stat(["00001"], metric="pe")
        pb = ad.build_hk_peer_stat(["00001"], metric="pb")
        sc = ad.build_hk_peer_stat(["00001"], metric="revenue_scale")
        self.assertAlmostEqual(pe.values[0], 14.72, places=6)
        self.assertAlmostEqual(pb.values[0], 3.004, places=6)
        self.assertAlmostEqual(sc.values[0], 401_243_000_000.0, places=0)
        for m in ("pe", "pb", "revenue_scale"):
            self.assertNotIn(m, ad.HK_RATIO_METRICS,
                             f"{m} 带币种或价格口径，不该被当成比率")
        for m in ("op_margin", "net_margin", "gross_margin", "revenue_yoy"):
            self.assertIn(m, ad.HK_RATIO_METRICS)

    def test_a_missing_field_becomes_a_gap_not_a_zero(self):
        """汇丰不报毛利 —— 记缺口，别拿 0 拉低中位数。"""
        self._feed({"00001": _fake_row("甲公司", 毛利率=57.25),
                    "00002": _fake_row("乙公司", 净利率=10.0)})
        st = ad.build_hk_peer_stat(["00001", "00002"], metric="gross_margin")
        self.assertEqual(st.n, 1)
        self.assertEqual(st.values, [0.5725])
        self.assertTrue(any("乙公司" in g and "毛利率" in g for g in st.gaps),
                        f"缺口里应该说清是哪家缺哪个科目：{st.gaps}")

    def test_source_has_nothing_for_a_code(self):
        self._feed({})
        st = ad.build_hk_peer_stat(["00009"], metric="pe")
        self.assertEqual(st.n, 0)
        self.assertTrue(st.gaps)

    def test_an_unknown_metric_is_refused(self):
        with self.assertRaises(ValueError):
            ad.build_hk_peer_stat(["00001"], metric="ebitda_margin")

    def test_secucode_normalises_hk_codes(self):
        self.assertEqual(hf.secucode("700"), "00700.HK")
        self.assertEqual(hf.secucode("00700"), "00700.HK")
        self.assertEqual(hf.secucode("00700.hk"), "00700.HK")


if __name__ == "__main__":
    unittest.main()
