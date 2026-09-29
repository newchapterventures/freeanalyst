"""市值类倍数（P/E、P/B、市值/收入）—— 把两个**真跑踩出来的 bug** 钉住。

## 为什么单独一个文件
这两条错都属于"**算得出来、看不出错**"的那一类，正是本项目最不许出现的：

① **分母的期间必须按该指标自己的科目去找** ——
   一开始图省事统一用收入标签找期末，结果 BorgWarner 的收入标签只到 2022，
   P/B 的分母就取了 2022 年的权益，而价格是 2025-06-30：**差了三年**。

② **股数不能取晚于基准日的那个** ——
   股数写在申报封面上，封面日通常晚于财年期末。不筛就会把两个时点拼起来。

## 价格一律打桩
真价走外部源，测试不联网（联网那条在 `tests/test_prices.py` 里，需
`FREEANALYST_NET_TESTS=1`）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datasources import sec_edgar as se          # noqa: E402
from valuation import advisor as ad              # noqa: E402


def _facts(*, rev_ends=("2022-12-31",), ni_ends=("2024-12-31",),
           eq_ends=("2025-03-31",), shares=(("2025-06-30", 100.0),)) -> dict:
    """造一份最小的 companyfacts：只放测试要用到的标签。

    注意 `rev_ends` 默认只到 2022 —— 这正是 BorgWarner 当时的形状。
    """
    def dur(tag, ends, val=100.0):
        return {"units": {"USD": [
            {"start": e[:4] + "-01-01", "end": e, "val": val, "form": "10-K",
             "filed": e} for e in ends]}}
    def inst(tag, ends, val=100.0):
        return {"units": {"USD": [
            {"end": e, "val": val, "form": "10-Q", "filed": e} for e in ends]}}
    gaap = {
        se._REVENUE_TAGS[0]: dur(se._REVENUE_TAGS[0], rev_ends),
        se._NET_INCOME_TAGS[0]: dur(se._NET_INCOME_TAGS[0], ni_ends),
        se._EQUITY_TAGS[0]: inst(se._EQUITY_TAGS[0], eq_ends),
    }
    dei = {se._SHARES_TAGS[0]: {"units": {"shares": [
        {"end": e, "val": v, "form": "10-Q", "filed": e} for e, v in shares]}}}
    return {"facts": {"us-gaap": gaap, "dei": dei}}


class TestDenominatorPeriod(unittest.TestCase):
    """★ bug ①：分母的期末必须按**这个指标自己的科目**去找。"""

    def test_pb_uses_equity_period_not_revenue_period(self):
        f = _facts(rev_ends=("2022-12-31",), eq_ends=("2025-03-31",))
        self.assertEqual(ad._latest_period_end(f, "pb", "USD", None), "2025-03-31")

    def test_pe_uses_net_income_period(self):
        f = _facts(rev_ends=("2022-12-31",), ni_ends=("2024-12-31",))
        self.assertEqual(ad._latest_period_end(f, "pe", "USD", None), "2024-12-31")

    def test_price_to_revenue_uses_revenue_period(self):
        f = _facts(rev_ends=("2022-12-31",))
        self.assertEqual(ad._latest_period_end(f, "price_to_revenue", "USD", None),
                         "2022-12-31")

    def test_as_of_filters_out_later_filings(self):
        """基准日之后才申报的数不能用 —— 那是"当时还看不到"的信息。"""
        f = _facts(eq_ends=("2024-12-31", "2025-03-31"))
        self.assertEqual(ad._latest_period_end(f, "pb", "USD", "2025-01-01"),
                         "2024-12-31")


class TestSharesAsOf(unittest.TestCase):
    """★ bug ②：股数不许取晚于基准日的（封面日 ≠ 财年期末）。"""

    def test_shares_are_filtered_by_as_of(self):
        f = _facts(shares=(("2024-12-31", 100.0), ("2025-06-30", 120.0)))
        got = se.shares_outstanding(f, "2025-01-01")
        self.assertIsNotNone(got)
        self.assertEqual(got.observation.end, "2024-12-31")
        self.assertEqual(got.value, 100.0)

    def test_shares_without_as_of_take_the_latest(self):
        f = _facts(shares=(("2024-12-31", 100.0), ("2025-06-30", 120.0)))
        got = se.shares_outstanding(f)
        self.assertEqual(got.observation.end, "2025-06-30")

    def test_shares_note_says_which_date_it_is(self):
        """返回里必须带日期 —— 改天有人问"这个市值是哪天的"要答得出来。"""
        got = se.shares_outstanding(_facts(shares=(("2025-06-30", 120.0),)))
        self.assertIn("2025-06-30", got.note)


class TestMultiplesNeedAReferenceDate(unittest.TestCase):
    """价格模块**刻意**没有"最新价" —— 所以没基准日就必须说清、而不是猜。"""

    def test_missing_as_of_is_refused_with_a_reason(self):
        st = ad.build_peer_multiples([("0000001", "AAA", "AAA")], metric="pe",
                                     as_of=None)
        self.assertEqual(st.n, 0)
        self.assertTrue(st.gaps)
        self.assertIn("必须有基准日", st.gaps[0])


class TestNetDebt(unittest.TestCase):
    """净债务的口径 —— 它**直接决定倍数的大小**，所以两条最要命的错得钉住。"""

    def _f(self, *, total=None, current=None, noncurrent=None, cash=1000.0,
           lease=None):
        def inst(tag, val):
            return {"units": {"USD": [{"end": "2024-12-31", "val": val,
                                       "form": "10-K", "filed": "2025-02-01"}]}}
        gaap = {}
        if total is not None:
            gaap[se._DEBT_TOTAL_TAGS[0]] = inst(se._DEBT_TOTAL_TAGS[0], total)
        if current is not None:
            gaap[se._DEBT_CURRENT_TAGS[0]] = inst(se._DEBT_CURRENT_TAGS[0], current)
        if noncurrent is not None:
            gaap[se._DEBT_NONCURRENT_TAGS[0]] = inst(se._DEBT_NONCURRENT_TAGS[0], noncurrent)
        if lease is not None:
            gaap[se._FINANCE_LEASE_TAGS[0]] = inst(se._FINANCE_LEASE_TAGS[0], lease)
        if cash is not None:
            gaap[se._CASH_TAGS[0]] = inst(se._CASH_TAGS[0], cash)
        return {"facts": {"us-gaap": gaap}}

    def test_total_and_split_are_never_both_counted(self):
        """★ 两套长期债务写法都加了就是**双计** —— 倍数会直接算错。"""
        both = self._f(total=500.0, current=100.0, noncurrent=400.0)
        got = se.net_debt_of(both, "2024-12-31")
        self.assertIsNotNone(got)
        # 只认合计那套：500 − 1000 = −500（若双计会变成 −500 之外的错值）
        self.assertEqual(got.value, -500.0)

    def test_split_is_used_when_there_is_no_total(self):
        split = self._f(current=100.0, noncurrent=400.0)
        got = se.net_debt_of(split, "2024-12-31")
        self.assertIsNotNone(got)
        self.assertEqual(got.value, -500.0)

    def test_missing_debt_is_reported_not_filled_with_zero(self):
        """★ 一个债务科目都没有 → 返回缺（**绝不拿 0 当"无债"**）。

        拿 0 填会**系统性低估 EV**，而且从数字上看不出来。
        """
        got = se.net_debt_of(self._f(cash=1000.0), "2024-12-31")
        self.assertIsNone(got)

    def test_missing_cash_is_reported(self):
        got = se.net_debt_of(self._f(total=500.0, cash=None), "2024-12-31")
        self.assertIsNone(got)

    def test_composition_is_in_the_note(self):
        """净债务是"口径决定结果"的东西 —— 必须能逐项核对。"""
        got = se.net_debt_of(self._f(total=500.0, lease=50.0, cash=1000.0),
                             "2024-12-31")
        self.assertIn("组成", got.note)
        self.assertIn("融资租赁", got.note)
        self.assertIn("不含经营租赁", got.note)


if __name__ == "__main__":
    unittest.main()
