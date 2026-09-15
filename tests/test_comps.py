"""可比公司倍数测试。

## 这个文件锁住的核心是「什么时候你才知道那份财报」

在基准日做估值，你只能用**当时已经披露**的财报。

实测踩到的完整 bug 链条（Oracle）：

    第一版：直接取最新财年 → 用 2025-12 的股价配 2026-05 才结束的财报
            （用了当时还不存在的数据 —— 前视偏差）

    第二版：改成"最晚申报日 ≤ 基准日" → 所有历史财年都被排除，只剩 FY2023
            因为每个新财年的 10-K 都会重述前两年的对照数，
            `annual_series` 保留最晚申报，于是 FY2024 的数值
            挂在了 FY2026 的申报日上

    第三版：改用**首次申报日** → FY2025-05-31 filed 2025-06-18 ✓

两个方向都会错，而且都不报错。所以这里用固定装置把判定钉死。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valuation import comps  # noqa: E402


def _obs(start, end, val, filed):
    return {"start": start, "end": end, "val": val, "form": "10-K",
            "fy": 2025, "fp": "FY", "filed": filed, "accn": f"acc-{filed}"}


# 模拟一家 5 月 31 日结账的公司，每个新 10-K 都重述前两年对照数
FACTS = {
    "cik": 1234,
    "entityName": "Test May Co",
    "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            # FY2023：首次申报 2023-06-20，之后被 FY2024、FY2025 的 10-K 重述
            _obs("2022-06-01", "2023-05-31", 100.0, "2023-06-20"),
            _obs("2022-06-01", "2023-05-31", 100.0, "2024-06-20"),
            _obs("2022-06-01", "2023-05-31", 100.0, "2025-06-18"),
            # FY2024：首次申报 2024-06-20，之后被 FY2025、FY2026 重述
            _obs("2023-06-01", "2024-05-31", 120.0, "2024-06-20"),
            _obs("2023-06-01", "2024-05-31", 120.0, "2025-06-18"),
            _obs("2023-06-01", "2024-05-31", 120.0, "2026-06-22"),
            # FY2025：首次申报 2025-06-18
            _obs("2024-06-01", "2025-05-31", 150.0, "2025-06-18"),
            _obs("2024-06-01", "2025-05-31", 150.0, "2026-06-22"),
            # FY2026：2026-06-22 才申报 —— 基准日 2025-12-31 时还看不到
            _obs("2025-06-01", "2026-05-31", 180.0, "2026-06-22"),
        ]}},
        "OperatingIncomeLoss": {"units": {"USD": [
            _obs("2024-06-01", "2025-05-31", 40.0, "2025-06-18"),
            _obs("2025-06-01", "2026-05-31", 50.0, "2026-06-22"),
        ]}},
        "DepreciationDepletionAndAmortization": {"units": {"USD": [
            _obs("2024-06-01", "2025-05-31", 10.0, "2025-06-18"),
            _obs("2025-06-01", "2026-05-31", 12.0, "2026-06-22"),
        ]}},
        "LongTermDebtNoncurrent": {"units": {"USD": [
            {"end": "2025-05-31", "val": 50.0, "form": "10-K", "filed": "2025-06-18", "accn": "a"},
        ]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            {"end": "2025-05-31", "val": 20.0, "form": "10-K", "filed": "2025-06-18", "accn": "b"},
        ]}},
    }},
}

AS_OF = "2025-12-31"


class TestFirstFiledDates(unittest.TestCase):
    """首次申报日 ≠ 最晚申报日。搞混会让所有历史财年被排除。"""

    def test_uses_earliest_not_latest_filing(self):
        ff = comps._first_filed_by_fiscal_end(FACTS)
        self.assertEqual(ff["2023-05-31"], "2023-06-20")   # 不是 2025-06-18
        self.assertEqual(ff["2024-05-31"], "2024-06-20")   # 不是 2026-06-22
        self.assertEqual(ff["2025-05-31"], "2025-06-18")
        self.assertEqual(ff["2026-05-31"], "2026-06-22")

    def test_annual_series_keeps_latest_filing_instead(self):
        """对照：annual_series 保留的是最晚申报 —— 这正是第一版的坑。"""
        from datasources import sec_edgar as se
        s = se.annual_series(FACTS, "RevenueFromContractWithCustomerExcludingAssessedTax")
        by_end = {o.end: o.filed for o in s}
        self.assertEqual(by_end["2024-05-31"], "2026-06-22",
                         "去重保留最晚申报 —— 所以不能直接拿它判断可不可见")


class TestLookAheadBias(unittest.TestCase):
    """不能用基准日之后才披露的财报。"""

    def test_picks_latest_fiscal_year_already_disclosed(self):
        self.assertEqual(
            comps._latest_reported_fiscal_end(FACTS, AS_OF), "2025-05-31"
        )

    def test_does_not_pick_a_future_fiscal_year(self):
        """FY2026 是 2026-06-22 才披露的，基准日 2025-12-31 时还看不到。"""
        fe = comps._latest_reported_fiscal_end(FACTS, AS_OF)
        self.assertNotEqual(fe, "2026-05-31")

    def test_gives_an_earlier_year_at_an_earlier_as_of(self):
        self.assertEqual(
            comps._latest_reported_fiscal_end(FACTS, "2024-12-31"), "2024-05-31"
        )
        self.assertEqual(
            comps._latest_reported_fiscal_end(FACTS, "2023-12-31"), "2023-05-31"
        )

    def test_earlier_than_any_fiscal_year_returns_none(self):
        self.assertIsNone(comps._latest_reported_fiscal_end(FACTS, "2023-01-01"))

    def test_explicit_future_fiscal_end_is_flagged(self):
        """显式指定未来的财年，不拦，但必须明确警告。"""
        self.assertEqual(
            comps._fiscal_end_filed_after(FACTS, "2026-05-31", AS_OF), "2026-06-22"
        )
        self.assertIsNone(
            comps._fiscal_end_filed_after(FACTS, "2025-05-31", AS_OF)
        )


class TestCompsSummary(unittest.TestCase):
    def _entry(self, metric_value):
        from valuation.comps import CompsEntry
        e = CompsEntry("x", "1", "2025-05-31", "2025-12-31", 10.0, 100.0,
                       1000.0, 0.0, 1000.0, metric_value, 100.0, 0.5)
        return e

    def test_quantiles(self):
        s = comps.CompsSummary([self._entry(v) for v in (10.0, 20.0, 30.0)], "ebitda")
        self.assertEqual(s.values(), [10.0, 20.0, 30.0])
        self.assertAlmostEqual(s.median(), 20.0)
        self.assertAlmostEqual(s.quantile(0.25), 15.0)
        self.assertAlmostEqual(s.quantile(0.75), 25.0)

    def test_skips_missing_values(self):
        """缺的项是 None，不能当成 0 混进样本里。"""
        s = comps.CompsSummary(
            [self._entry(10.0), self._entry(None), self._entry(30.0)], "ebitda"
        )
        self.assertEqual(s.values(), [10.0, 30.0])
        self.assertEqual(s.median(), 20.0)

    def test_empty_sample(self):
        s = comps.CompsSummary([], "ebitda")
        self.assertIsNone(s.median())
        self.assertIn("无可用数据", s.render())

    def test_single_value(self):
        s = comps.CompsSummary([self._entry(12.0)], "ebitda")
        self.assertEqual(s.median(), 12.0)


class TestMultiplesNeverDivideByZero(unittest.TestCase):
    def _entry(self, ev, ebitda, revenue):
        from valuation.comps import CompsEntry
        return CompsEntry("x", "1", "2025-05-31", "2025-12-31", 10.0, 100.0,
                          ev, 0.0, ev, ebitda, revenue, None)

    def test_zero_or_negative_ebitda_gives_no_multiple(self):
        """亏损公司的 EV/EBITDA 没有意义，必须返回 None 而不是负数。"""
        self.assertIsNone(self._entry(1000.0, -5.0, 100.0).ev_ebitda)
        self.assertIsNone(self._entry(1000.0, 0.0, 100.0).ev_ebitda)

    def test_missing_ev_gives_no_multiple(self):
        self.assertIsNone(self._entry(None, 50.0, 100.0).ev_ebitda)

    def test_valid_case(self):
        self.assertAlmostEqual(self._entry(1000.0, 50.0, 100.0).ev_ebitda, 20.0)
        self.assertAlmostEqual(self._entry(1000.0, 50.0, 100.0).ev_revenue, 10.0)


if __name__ == "__main__":
    unittest.main()
