"""SEC EDGAR 数据源测试。

## 这个文件锁住的是两个真实踩过的坑

**坑 1：年度值和季度值混在一起。**
同一个 XBRL 标签、同一个期末，会有多条数据点，区别在 start→end 的时长。
不过滤的话，一条 90 天的季度值会被当成年度值。

**坑 2：标签会废弃，数据会静默停在旧年份。**
Apple 的 `Revenues` 标签数据停在 2018 年 —— 它后来换用了
`RevenueFromContractWithCustomerExcludingAssessedTax`。
只试一个标签的话，拿到的是**八年前的数**，而且不报错。

两个坑的共同点：**不报错，但结果是错的。** 所以这里用固定装置
（fixture）把判定逻辑钉死，不依赖网络。

## 网络测试

需要真实请求的用例标了 `@unittest.skipUnless`，
要设 `SEC_USER_AGENT` 且显式开 `FREEANALYST_NET_TESTS=1` 才跑。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasources import sec_edgar as se  # noqa: E402


def _row(start, end, val, form="10-K", filed="2025-11-01"):
    return {"start": start, "end": end, "val": val, "form": form,
            "fy": 2025, "fp": "FY", "filed": filed, "accn": "0000000-25-000001"}


# 一个仿真的 EDGAR facts 结构，刻意埋了上面两个坑
FIXTURE = {
    "cik": 999999,
    "entityName": "Test Co",
    "facts": {
        "us-gaap": {
            # 坑 2：老标签，数据停在 2018
            "Revenues": {"units": {"USD": [
                _row("2016-01-01", "2016-12-31", 100.0, filed="2017-02-01"),
                _row("2017-01-01", "2017-12-31", 110.0, filed="2018-02-01"),
                _row("2018-01-01", "2018-12-31", 120.0, filed="2019-02-01"),
            ]}},
            # 新标签，数据到 2025
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                _row("2023-01-01", "2023-12-31", 300.0, filed="2024-02-01"),
                _row("2024-01-01", "2024-12-31", 330.0, filed="2025-02-01"),
                _row("2025-01-01", "2025-12-31", 360.0, filed="2026-02-01"),
                # 下面的季度值挂在同一个期末上 —— 坑 1
                _row("2025-10-01", "2025-12-31", 95.0, form="10-Q", filed="2026-02-01"),
                _row("2025-01-01", "2025-09-30", 265.0, form="10-Q", filed="2025-11-01"),
            ]}},
            # 重述：同一期末两条，数值不同
            "SalesRevenueNet": {"units": {"USD": [
                _row("2022-01-01", "2022-12-31", 280.0, filed="2023-02-01"),
                _row("2022-01-01", "2022-12-31", 285.0, filed="2024-06-01"),  # 重述
            ]}},
            "OperatingIncomeLoss": {"units": {"USD": [
                _row("2025-01-01", "2025-12-31", 72.0, filed="2026-02-01"),
            ]}},
            # 时点科目：没有 start
            "Assets": {"units": {"USD": [
                {"end": "2024-12-31", "val": 900.0, "form": "10-K", "filed": "2025-02-01", "accn": "x"},
                {"end": "2025-12-31", "val": 980.0, "form": "10-K", "filed": "2026-02-01", "accn": "y"},
            ]}},
        }
    },
}


class TestDurationFiltering(unittest.TestCase):
    """坑 1：年度值和季度值必须分开。"""

    TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"

    def test_annual_excludes_quarterly(self):
        annual = se.annual_series(FIXTURE, self.TAG)
        values = [o.value for o in annual]
        self.assertEqual(values, [300.0, 330.0, 360.0])
        # 季度值是 95 和 265，绝不能出现在年度序列里
        self.assertNotIn(95.0, values)
        self.assertNotIn(265.0, values)

    def test_without_filter_the_quarter_leaks_in(self):
        """反向验证：不做时长过滤就会混进来。

        这条测试的存在，是为了说明为什么 duration 参数不是可选项。
        """
        raw = se.extract_series(FIXTURE, self.TAG)
        values = [o.value for o in raw]
        self.assertIn(95.0, values, "不做过滤时季度值确实会出现——这就是坑")
        self.assertEqual(len(raw), 5)
        self.assertEqual(len(se.annual_series(FIXTURE, self.TAG)), 3)

    def test_quarter_filter(self):
        """只拿到真正的单季（90 天）。

        装置里 265.0 那条是 2025-01-01 → 2025-09-30 = 272 天，
        **是 9 个月的年内累计，不是单季** —— 第一版测试把它当季度，
        是测试写错了，不是代码错了。
        """
        q = se.extract_series(FIXTURE, self.TAG, duration="quarter")
        self.assertEqual([o.value for o in q], [95.0])

    def test_ytd_periods_are_their_own_bucket(self):
        """三季报的 9 个月累计，年度和季度两档都筛不到 —— 必须单列。"""
        ytd9 = se.extract_series(FIXTURE, self.TAG, duration="ytd9")
        self.assertEqual([o.value for o in ytd9], [265.0])

        # 反向验证：只用 annual + quarter 两档的话，272 天那条两边都不落
        annual = se.extract_series(FIXTURE, self.TAG, duration="annual")
        quarter = se.extract_series(FIXTURE, self.TAG, duration="quarter")
        covered = {o.value for o in annual} | {o.value for o in quarter}
        self.assertNotIn(265.0, covered, "两档筛选覆盖不到 9 个月累计——这就是缺口")

    def test_point_in_time_excluded_from_annual(self):
        """时点科目（总资产）没有 start，不该被当成年度值。"""
        self.assertEqual(se.annual_series(FIXTURE, "Assets"), [])
        # 但不过滤时能取到
        self.assertEqual(len(se.extract_series(FIXTURE, "Assets")), 2)

    def test_unknown_duration_raises(self):
        with self.assertRaises(ValueError):
            se.extract_series(FIXTURE, self.TAG, duration="monthly")


class TestDeduplication(unittest.TestCase):
    """重述：同一期末多条记录。"""

    def test_keeps_latest_filed(self):
        s = se.annual_series(FIXTURE, "SalesRevenueNet")
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0].value, 285.0)  # 2024-06-01 申报的那版

    def test_dedupe_off_shows_restatement(self):
        s = se.annual_series(FIXTURE, "SalesRevenueNet", dedupe=False)
        self.assertEqual([o.value for o in s], [280.0, 285.0])


class TestTagFallback(unittest.TestCase):
    """坑 2：标签废弃后数据会静默停在旧年份。"""

    def test_revenue_prefers_current_tag_not_deprecated_one(self):
        rev = se.derive_revenue(FIXTURE, "2025-12-31")
        self.assertIsNotNone(rev)
        # 必须拿到 360（新标签），不能是 120（老标签 2018 年的值）
        self.assertEqual(rev.value, 360.0)
        self.assertEqual(rev.tag, "RevenueFromContractWithCustomerExcludingAssessedTax")
        self.assertNotEqual(rev.value, 120.0)

    def test_only_deprecated_tag_available(self):
        """新标签没有该期末数据时，回退到老标签 —— 但要标出来用的是哪个。"""
        rev = se.derive_revenue(FIXTURE, "2017-12-31")
        self.assertEqual(rev.tag, "Revenues")
        self.assertEqual(rev.value, 110.0)

    def test_revenue_returns_none_when_absent(self):
        self.assertIsNone(se.derive_revenue(FIXTURE, "1999-12-31"))


class TestNeverGuess(unittest.TestCase):
    """算不出来必须返回 None，不能拿别的东西填。"""

    def test_ebitda_none_when_depreciation_missing(self):
        # 装置里有 OperatingIncomeLoss，但没有任何 D&A 标签
        self.assertIsNone(
            se.derive_ebitda(FIXTURE, "2025-12-31"),
            "缺折旧摊销时必须返回 None。用行业均值填充是最阴险的错误：不报错，但结果全错。",
        )

    def test_ebitda_computes_when_both_present(self):
        import copy
        f = copy.deepcopy(FIXTURE)
        f["facts"]["us-gaap"]["DepreciationDepletionAndAmortization"] = {"units": {"USD": [
            _row("2025-01-01", "2025-12-31", 18.0, filed="2026-02-01"),
        ]}}
        eb = se.derive_ebitda(f, "2025-12-31")
        self.assertEqual(eb.value, 90.0)  # 72 + 18
        self.assertIn("OperatingIncomeLoss", eb.tag)
        self.assertIn("DepreciationDepletionAndAmortization", eb.tag)

    def test_ebitda_none_when_operating_income_missing(self):
        import copy
        f = copy.deepcopy(FIXTURE)
        del f["facts"]["us-gaap"]["OperatingIncomeLoss"]
        self.assertIsNone(se.derive_ebitda(f, "2025-12-31"))


class TestUserAgent(unittest.TestCase):
    """SEC 的 403 不解释原因，所以本地必须先给出可操作的提示。"""

    def test_missing_email_raises_with_instructions(self):
        old = os.environ.pop(se.ENV_KEY, None)
        try:
            with self.assertRaises(se.SecEdgarError) as cm:
                se.user_agent()
            msg = str(cm.exception)
            self.assertIn("403", msg)
            self.assertIn(se.ENV_KEY, msg)
        finally:
            if old is not None:
                os.environ[se.ENV_KEY] = old

    def test_email_present_passes(self):
        old = os.environ.get(se.ENV_KEY)
        os.environ[se.ENV_KEY] = "Test Org test@example.com"
        try:
            self.assertEqual(se.user_agent(), "Test Org test@example.com")
        finally:
            if old is None:
                os.environ.pop(se.ENV_KEY, None)
            else:
                os.environ[se.ENV_KEY] = old


class TestCikNormalization(unittest.TestCase):
    def test_pads_to_ten_digits(self):
        self.assertEqual(se._normalize_cik("320193"), "0000320193")
        self.assertEqual(se._normalize_cik(320193), "0000320193")
        self.assertEqual(se._normalize_cik("0000320193"), "0000320193")

    def test_rejects_non_numeric(self):
        with self.assertRaises(se.SecEdgarError):
            se._normalize_cik("AAPL")


class TestGunzip(unittest.TestCase):
    """坑 3：请求了 gzip 却不解压 —— 报 UnicodeDecodeError，看着像编码问题。"""

    def test_detects_and_decompresses(self):
        import gzip
        payload = b'{"ok": true}'
        self.assertEqual(se._maybe_gunzip(gzip.compress(payload)), payload)

    def test_passes_through_plain(self):
        payload = b'{"ok": true}'
        self.assertEqual(se._maybe_gunzip(payload), payload)


@unittest.skipUnless(
    os.environ.get("FREEANALYST_NET_TESTS") == "1" and "@" in os.environ.get(se.ENV_KEY, ""),
    "需要真实网络：设 SEC_USER_AGENT 并 FREEANALYST_NET_TESTS=1",
)
class TestAgainstRealSec(unittest.TestCase):
    """对真实 SEC 数据的回归。跑之前先确认数字和公司披露一致。"""

    def test_apple_revenue_matches_filings(self):
        facts = se.company_facts("0000320193")
        s = se.annual_series(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
        got = {o.end: o.value for o in s}
        # Apple 年报披露的口径（单位：美元）
        self.assertAlmostEqual(got["2023-09-30"] / 1e9, 383.285, places=2)
        self.assertAlmostEqual(got["2024-09-28"] / 1e9, 391.035, places=2)

    def test_frames_returns_thousands_of_companies(self):
        rows = se.frames("Assets", "CY2024Q4I")
        self.assertGreater(len(rows), 1000)


if __name__ == "__main__":
    unittest.main()
