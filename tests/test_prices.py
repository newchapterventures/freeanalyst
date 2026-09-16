"""价格数据源测试。

## 这个文件锁住的两条纪律

**① 估值用基准日的价，不用今天的价。**

实测差异：某白酒公司 2025-12-31 收盘 1,377.18，最近交易日 1,276.80 ——
**差 7.3%**。用错的话市值直接差这么多，而且没有任何提示。

所以 `close_on()` 有一条不能破的规则：
**找不到不晚于基准日的数据，就返回 None，绝不能回退到"最新的价"。**

**② 美股前缀不能靠猜。**

看起来像"105=纳斯达克、106=纽交所"，但沃尔玛是纽交所上市的却挂在 105 下。
所以只能按 105→106→107 顺序探测，命中的缓存起来。

## 网络测试

需要真实请求的用例标了 skipUnless，要 `FREEANALYST_NET_TESTS=1`。
其余用固定装置，不依赖网络。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasources import prices as p  # noqa: E402


# 每行是东财返回的一条：日期,开,收,高,低,量
FIXTURE_ROWS = [
    "2025-12-26,1400.00,1405.50,1410.00,1398.00,30000",
    "2025-12-29,1405.00,1398.20,1408.00,1395.00,28000",
    "2025-12-30,1398.00,1390.40,1400.00,1388.00,26000",
    "2025-12-31,1390.00,1377.18,1392.00,1375.00,25000",
    "2026-01-05,1377.00,1397.98,1400.00,1376.00,31000",
]


class _Patched:
    """把最底层的 `_fetch_rows` 换掉，避免测试依赖网络。

    **只换最底层**，这样 `_fetch` 里的前缀探测回退逻辑仍然会被真正执行 ——
    如果换成 patch `_fetch`，测的就是假函数而不是真逻辑了。
    """

    def __init__(self, rows=FIXTURE_ROWS, working_prefix=None):
        """working_prefix=None 表示「哪个前缀都当它有数据」——
        不关心前缀探测的测试用默认值即可。"""
        self.rows = rows
        self.working_prefix = working_prefix
        self.calls: list[dict] = []

    def __enter__(self):
        self._orig = p._fetch_rows
        outer = self

        def fake(secid, beg, end, adjust):
            outer.calls.append({"secid": secid, "beg": beg,
                                "end": end, "adjust": adjust})
            prefix = secid.split(".", 1)[0]
            if outer.working_prefix is not None and prefix != outer.working_prefix:
                return None          # 模拟「这个前缀没数据」
            rows = [r for r in outer.rows if beg <= r.split(",")[0] <= end]
            return rows or None

        p._fetch_rows = fake
        return self

    def __exit__(self, *a):
        p._fetch_rows = self._orig

    def prefixes_tried(self) -> list[str]:
        return [c["secid"].split(".", 1)[0] for c in self.calls]


class _IsolatedPrefixCache:
    """把前缀缓存指到临时文件。

    不加这层的话，测试会依赖本机缓存里已有什么 —— 联网跑过一次之后
    `ORCL→106` 被记住，断言"首选是 105"就会失败。行为是对的，测试不隔离。
    """

    def __enter__(self):
        import tempfile
        self._orig = p._PREFIX_CACHE
        self._tmp = Path(tempfile.mkdtemp()) / "us_prefix.json"
        p._PREFIX_CACHE = self._tmp
        return self

    def __exit__(self, *a):
        p._PREFIX_CACHE = self._orig


class TestSecidResolution(unittest.TestCase):
    """A 股港股前缀确定；美股必须探测。"""

    def test_deterministic_prefixes(self):
        self.assertEqual(p.resolve_secid("sh", "600519"), "1.600519")
        self.assertEqual(p.resolve_secid("sz", "000001"), "0.000001")
        self.assertEqual(p.resolve_secid("sz", "300750"), "0.300750")
        self.assertEqual(p.resolve_secid("hk", "00700"), "116.00700")

    def test_case_insensitive_code(self):
        with _IsolatedPrefixCache():
            self.assertEqual(p.resolve_secid("us", "aapl"), "105.AAPL")

    def test_unknown_market_raises_with_options(self):
        with self.assertRaises(p.PriceError) as cm:
            p.resolve_secid("bj", "600519")
        msg = str(cm.exception)
        for opt in ("sh", "sz", "us", "hk"):
            self.assertIn(opt, msg)

    def test_empty_code_raises(self):
        with self.assertRaises(p.PriceError):
            p.resolve_secid("sh", "  ")

    def test_us_candidates_try_three_prefixes(self):
        with _IsolatedPrefixCache():
            self.assertEqual(
                p._candidate_secids("us", "ORCL"),
                ["105.ORCL", "106.ORCL", "107.ORCL"],
            )

    def test_non_us_has_single_candidate(self):
        self.assertEqual(p._candidate_secids("sh", "600519"), ["1.600519"])

    def test_cached_prefix_is_tried_first(self):
        """探测过的前缀会排到最前 —— 省掉重复探测。"""
        with _IsolatedPrefixCache() as iso:
            iso._tmp.parent.mkdir(parents=True, exist_ok=True)
            iso._tmp.write_text('{"ORCL": "106"}', encoding="utf-8")
            self.assertEqual(
                p._candidate_secids("us", "ORCL"),
                ["106.ORCL", "105.ORCL", "107.ORCL"],
            )


class TestUsPrefixFallback(unittest.TestCase):
    """美股前缀必须探测时回退 —— 沃尔玛走在 105，甲骨文走在 106。

    这里测的是 `_fetch` 里的真实回退逻辑（只把最底层取数替换掉了）。
    """

    def test_falls_back_until_a_prefix_works(self):
        with _IsolatedPrefixCache(), _Patched(working_prefix="106") as f:
            bar = p.close_on("us", "ORCL", "2025-12-31")
        self.assertIsNotNone(bar)
        self.assertTrue(bar.secid.startswith("106."))
        # 105 试过没数据，才轮到 106
        self.assertEqual(f.prefixes_tried(), ["105", "106"])

    def test_no_fallback_when_first_prefix_works(self):
        with _IsolatedPrefixCache(), _Patched(working_prefix="105") as f:
            bar = p.close_on("us", "AAPL", "2025-12-31")
        self.assertIsNotNone(bar)
        self.assertEqual(f.prefixes_tried(), ["105"])  # 不该多试

    def test_default_first_candidate_is_105(self):
        """如果只用首选前缀 105，甲骨文会取不到价 —— 这就是回退存在的理由。"""
        with _IsolatedPrefixCache():
            self.assertEqual(p._candidate_secids("us", "AAPL")[0], "105.AAPL")

    def test_all_prefixes_fail(self):
        with _IsolatedPrefixCache(), _Patched(working_prefix="999") as f:
            self.assertIsNone(p.close_on("us", "ORCL", "2025-12-31"))
        self.assertEqual(f.prefixes_tried(), ["105", "106", "107"])

    def test_non_us_market_does_not_probe(self):
        with _Patched(working_prefix="1") as f:
            p.close_on("sh", "600519", "2025-12-31")
        self.assertEqual(f.prefixes_tried(), ["1"])


class TestAsOfDate(unittest.TestCase):
    """核心纪律：取不晚于基准日的最近一个交易日。"""

    def test_exact_date(self):
        with _Patched():
            bar = p.close_on("sh", "600519", "2025-12-31")
        self.assertEqual(bar.date, "2025-12-31")
        self.assertEqual(bar.close, 1377.18)

    def test_never_returns_a_date_after_as_of(self):
        with _Patched():
            bar = p.close_on("sh", "600519", "2025-12-30")
        self.assertEqual(bar.date, "2025-12-30")
        self.assertLessEqual(bar.date, "2025-12-30")

    def test_falls_back_to_earlier_trading_day_on_holiday(self):
        """基准日是休市日 → 回退到之前最近交易日，不能往后取。"""
        with _Patched():
            bar = p.close_on("sh", "600519", "2026-01-01")  # 元旦休市
        self.assertEqual(bar.date, "2025-12-31")

    def test_returns_none_instead_of_latest_when_only_future_data(self):
        """**绝不能回退到"最新的价"** —— 那是另一种口径的错误。

        装置里最早的日期是 2025-12-26。问 2025-12-25，
        必须返回 None，而不是拿 2025-12-26 或 2026-01-05 来填。
        """
        with _Patched():
            self.assertIsNone(p.close_on("sh", "600519", "2025-12-25"))

    def test_bad_date_format_raises(self):
        with self.assertRaises(p.PriceError):
            p.close_on("sh", "600519", "2025/12/31")


class TestAdjustment(unittest.TestCase):
    """估值要用不复权价 —— 前复权价配当期股本会算错市值。"""

    def test_defaults_to_unadjusted(self):
        with _Patched() as f:
            p.close_on("sh", "600519", "2025-12-31")
        self.assertEqual(f.calls[0]["adjust"], 0)

    def test_adjust_is_passed_through(self):
        with _Patched() as f:
            p.close_on("sh", "600519", "2025-12-31", adjust=1)
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
        with _Patched() as f:
            p.close_on("sh", "600519", "2026-01-05", lookback_days=15)
        self.assertEqual(f.calls[0]["beg"], "2025-12-21")
        self.assertEqual(f.calls[0]["end"], "2026-01-05")

    def test_window_is_configurable(self):
        with _Patched() as f:
            p.close_on("sh", "600519", "2026-01-05", lookback_days=40)
        self.assertEqual(f.calls[0]["beg"], "2025-11-26")


class TestNoData(unittest.TestCase):
    """区分「这个代码不存在」和「有代码但这个区间没数据」。"""

    def test_all_prefixes_fail_returns_none(self):
        with _IsolatedPrefixCache(), _Patched(rows=[], working_prefix="999"):
            self.assertIsNone(p.close_on("us", "ZZZZ", "2025-12-31"))

    def test_empty_range_returns_empty_list(self):
        with _Patched():
            self.assertEqual(p.daily_closes("sh", "600519", "2020-01-01", "2020-01-05"), [])


class TestConnectionDrops(unittest.TestCase):
    """东财会在掐断连接时不给任何响应 —— 不能把它当成"没有数据"。

    这带来一个危险的歧义：拿不到数据可能是 (a) 代码不存在 (b) 网络断了
    (c) 被限流。静默返回"无数据"会让可比集合悄悄少几家公司。
    """

    def test_retries_then_raises_with_all_three_possibilities(self):
        import http.client
        attempts = []

        def boom(*a, **k):
            attempts.append(1)
            raise http.client.RemoteDisconnected("Remote end closed connection")

        orig = p.net.guarded_get
        p.net.guarded_get = boom
        try:
            with self.assertRaises(p.PriceError) as cm:
                p._fetch_rows("1.999999", "2025-12-01", "2025-12-31", 0)
        finally:
            p.net.guarded_get = orig

        self.assertEqual(len(attempts), 3, "必须重试 3 次才放弃")
        msg = str(cm.exception)
        self.assertIn("代码", msg)
        self.assertIn("网络", msg)
        self.assertIn("限流", msg)

    def test_succeeds_after_a_transient_drop(self):
        import http.client
        state = {"n": 0}
        rows = json.dumps({"data": {"klines": ["2025-12-31,1390.00,1377.18,1392.00,1375.00,25000"]}})

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                raise http.client.RemoteDisconnected("boom")
            return rows.encode("utf-8")

        orig = p.net.guarded_get
        p.net.guarded_get = flaky
        try:
            out = p._fetch_rows("1.999998", "2025-12-01", "2025-12-31", 0)
        finally:
            p.net.guarded_get = orig

        self.assertIsNotNone(out)
        self.assertEqual(len(out), 1)


@unittest.skipUnless(
    os.environ.get("FREEANALYST_NET_TESTS") == "1",
    "需要真实网络：设 FREEANALYST_NET_TESTS=1",
)
class TestAgainstRealPrices(unittest.TestCase):
    """对真实行情的回归。价格会变，所以断言口径不断言数值。"""

    def test_a_share_us_and_nyse_all_resolve(self):
        """含一个纳斯达克（AAPL）和一个纽交所（ORCL）—— 验证前缀探测。"""
        for market, code, expect_prefix in [
            ("sh", "600519", "1."),
            ("hk", "00700", "116."),
            ("us", "AAPL", "105."),
            ("us", "ORCL", "106."),
        ]:
            bar = p.close_on(market, code, "2025-12-31")
            self.assertIsNotNone(bar, f"{market}/{code} 取不到价")
            self.assertTrue(bar.secid.startswith(expect_prefix),
                            f"{market}/{code} 命中 {bar.secid}，预期 {expect_prefix}")
            self.assertLessEqual(bar.date, "2025-12-31")

    def test_realtime_price_differs_from_as_of_price(self):
        """把"用实时价"的代价量化下来 —— 这是设计决策的依据。"""
        asof = p.close_on("sh", "600519", "2025-12-31")
        live = p.latest_close("sh", "600519")
        assert asof is not None and live is not None
        self.assertGreater(live.date, asof.date)
        print(f"\n  基准日 {asof.date} {asof.close:,.2f}  vs  "
              f"最近 {live.date} {live.close:,.2f}  "
              f"差 {(live.close / asof.close - 1) * 100:+.1f}%")

    def test_nonexistent_code_does_not_yield_a_price(self):
        """不存在的代码绝不能给出一个价格。

        ## 这个测试为什么接受两种结果

        实测发现东财对不存在的代码有**两种间歇性行为**：
          - 直接掐断连接（不给任何响应）→ 重试 3 次后抛 PriceError
          - 回 `{"rc":100, "data":null}` → 干净地返回 None

        两种都对，取决于对方这次怎么处理。所以测试断言的是**真正的不变量**：

            **绝不会凭空给出一个价格。**

        把断言写成"必须抛异常"或"必须返回 None"，都是在假定对方的随机行为。
        """
        try:
            bar = p.close_on("sh", "999999", "2025-12-31")
        except p.PriceError as e:
            # 掐连接那条路：报错信息比 None 更有用
            self.assertIn("代码", str(e))
            return
        self.assertIsNone(bar, "不存在的代码绝不能给出一个价格")


if __name__ == "__main__":
    unittest.main()
