"""价格数据源测试。

## 这个文件锁住的核心纪律

**估值用基准日的价，不用今天的价。**

实测差异：贵州茅台 2025-12-31 收盘 1,377.18，最近交易日 1,276.80 ——
**差 7.3%**。用错的话，市值直接差这么多，而且没有任何提示。

所以 `close_on()` 有一条不能破的规则：
**找不到不晚于基准日的数据，就返回 None，绝不能回退到"最新的价"。**
回退到最新价是一种更隐蔽的错 —— 它看起来有数，但口径是错的。

## 网络测试

需要真实请求的用例标了 skipUnless，要 `FREEANALYST_NET_TESTS=1`。
其余用固定装置，不依赖网络。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasources import prices as p  # noqa: E402


# 每个元素是东财返回的一行：日期,开,收,高,低,量
FIXTURE_ROWS = [
    "2025-12-26,1400.00,1405.50,1410.00,1398.00,30000",
    "2025-12-29,1405.00,1398.20,1408.00,1395.00,28000",
    "2025-12-30,1398.00,1390.40,1400.00,1388.00,26000",
    "2025-12-31,1390.00,1377.18,1392.00,1375.00,25000",
    "2026-01-05,1377.00,1397.98,1400.00,1376.00,31000",
]


class _PatchedFetch:
    """把 _fetch 换掉，避免测试依赖网络。"""

    def __init__(self, rows=FIXTURE_ROWS, calls=None):
        self.rows = rows
        self.calls = calls if calls is not None else []

    def __enter__(self):
        self._orig = p._fetch

        def fake(secid, beg, end, adjust):
            self.calls.append({"secid": secid, "beg": beg, "end": end, "adjust": adjust})
            return [r for r in self.rows if beg <= r.split(",")[0] <= end]

        p._fetch = fake
        return self

    def __exit__(self, *a):
        p._fetch = self._orig


class TestMarketPrefix(unittest.TestCase):
    def test_prefixes(self):
        self.assertEqual(p.resolve_secid("sh", "600519"), "1.600519")
        self.assertEqual(p.resolve_secid("sz", "000001"), "0.000001")
        self.assertEqual(p.resolve_secid("sz", "300750"), "0.300750")
        self.assertEqual(p.resolve_secid("us", "aapl"), "105.AAPL")
        self.assertEqual(p.resolve_secid("hk", "00700"), "116.00700")

    def test_unknown_market_raises_with_options(self):
        with self.assertRaises(p.PriceError) as cm:
            p.resolve_secid("bj", "600519")
        self.assertIn("sh", str(cm.exception))

    def test_empty_code_raises(self):
        with self.assertRaises(p.PriceError):
            p.resolve_secid("sh", "  ")


class TestAsOfDate(unittest.TestCase):
    """核心纪律：取不晚于基准日的最近一个交易日。"""

    def test_exact_date(self):
        with _PatchedFetch():
            bar = p.close_on("1.600519", "2025-12-31")
        self.assertEqual(bar.date, "2025-12-31")
        self.assertEqual(bar.close, 1377.18)

    def test_never_returns_a_date_after_as_of(self):
        with _PatchedFetch():
            bar = p.close_on("1.600519", "2025-12-30")
        self.assertEqual(bar.date, "2025-12-30")
        self.assertLessEqual(bar.date, "2025-12-30")

    def test_falls_back_to_earlier_trading_day_on_holiday(self):
        """基准日是休市日 → 回退到之前最近交易日，不能往后取。"""
        with _PatchedFetch():
            bar = p.close_on("1.600519", "2026-01-01")  # 元旦休市
        self.assertEqual(bar.date, "2025-12-31")

    def test_returns_none_instead_of_latest_when_only_future_data(self):
        """**绝不能回退到"最新的价"** —— 那是另一种口径的错误。

        装置里最早的日期是 2025-12-26。问 2025-12-25 的话，
        必须返回 None，而不是拿 2025-12-26 或 2026-01-05 来填。
        """
        with _PatchedFetch():
            self.assertIsNone(p.close_on("1.600519", "2025-12-25"))

    def test_bad_date_format_raises(self):
        with self.assertRaises(p.PriceError):
            p.close_on("1.600519", "2025/12/31")


class TestAdjustment(unittest.TestCase):
    """估值要用不复权价 —— 前复权价配当期股本会算错市值。"""

    def test_defaults_to_unadjusted(self):
        with _PatchedFetch() as f:
            p.close_on("1.600519", "2025-12-31")
        self.assertEqual(f.calls[0]["adjust"], 0)

    def test_adjust_is_passed_through(self):
        with _PatchedFetch() as f:
            p.close_on("1.600519", "2025-12-31", adjust=1)
        self.assertEqual(f.calls[0]["adjust"], 1)


class TestParsing(unittest.TestCase):
    def test_skips_malformed_lines(self):
        rows = [
            "2025-12-31,1390.00,1377.18,1392.00,1375.00,25000",
            "garbage",
            "2025-12-30,1398.00,notanumber,1400.00,1388.00,26000",
            "2025-12-29,1405.00,1398.20,1408.00,1395.00,28000",
        ]
        bars = p._parse_rows(rows, "1.600519", 0)
        self.assertEqual([b.date for b in bars], ["2025-12-29", "2025-12-31"])
        self.assertEqual([b.close for b in bars], [1398.20, 1377.18])

    def test_empty_rows(self):
        self.assertEqual(p._parse_rows([], "x", 0), [])

    def test_sorts_by_date(self):
        rows = [
            "2026-01-05,1377.00,1397.98,1400.00,1376.00,31000",
            "2025-12-26,1400.00,1405.50,1410.00,1398.00,30000",
        ]
        bars = p._parse_rows(rows, "x", 0)
        self.assertEqual([b.date for b in bars], ["2025-12-26", "2026-01-05"])


class TestLookbackWindow(unittest.TestCase):
    def test_window_covers_preceding_holidays(self):
        """lookback 要够长，春节/国庆长假才回退得动。"""
        with _PatchedFetch() as f:
            p.close_on("1.600519", "2026-01-05", lookback_days=15)
        self.assertEqual(f.calls[0]["beg"], "2025-12-21")
        self.assertEqual(f.calls[0]["end"], "2026-01-05")

    def test_window_is_configurable(self):
        with _PatchedFetch() as f:
            p.close_on("1.600519", "2026-01-05", lookback_days=40)
        self.assertEqual(f.calls[0]["beg"], "2025-11-26")


@unittest.skipUnless(
    os.environ.get("FREEANALYST_NET_TESTS") == "1",
    "需要真实网络：设 FREEANALYST_NET_TESTS=1",
)
class TestAgainstRealPrices(unittest.TestCase):
    """对真实行情的回归。数字会变，所以只断言结构和口径，不硬编码价格。"""

    def test_a_share_us_hk_all_return_something(self):
        for secid in ("1.600519", "105.AAPL", "116.00700"):
            bar = p.close_on(secid, "2025-12-31")
            self.assertIsNotNone(bar, f"{secid} 取不到 2025-12-31 的价")
            self.assertGreater(bar.close, 0)
            self.assertLessEqual(bar.date, "2025-12-31")

    def test_realtime_price_differs_from_as_of_price(self):
        """把"用实时价"的代价量化下来 —— 这是设计决策的依据。"""
        asof = p.close_on("1.600519", "2025-12-31")
        live = p.latest_close("1.600519")
        self.assertIsNotNone(asof)
        self.assertIsNotNone(live)
        self.assertGreater(live.date, asof.date)
        print(f"\n  基准日 {asof.date} {asof.close:,.2f}  vs  "
              f"最近 {live.date} {live.close:,.2f}  "
              f"差 {(live.close / asof.close - 1) * 100:+.1f}%")

    def test_nonexistent_code_returns_none(self):
        self.assertIsNone(p.close_on("1.999999", "2025-12-31"))


if __name__ == "__main__":
    unittest.main()
