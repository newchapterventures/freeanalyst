"""第 4 步 · 可比公司接口（`/api/comps`）—— **只测不联网的部分**。

## 为什么这么测
这个接口的活儿是「取同行基本面分布」，取数要联网（SEC EDGAR）。
测试里**不联网**：把 `sec_edgar` / `advisor` 打桩，
验的是**接口自己的职责** —— 输入校验、诚实报错、样本不足的判定、字段形状。

联网那部分已经有自己的测试与实测（`tests/test_comps_workflow.py` 等），
这里再连一次网只会让测试变慢、变脆，还测不出新东西。

## 三条必须钉住的纪律
1. **不给倍数** —— EDGAR 没有股价，市值/EV/EV·EBITDA 算不出来；
   接口的返回里**不许**出现这些字段（假装能算是最坏的一类错）。
2. **样本 < 3 家要明说** —— 2 家的"中位数"没有意义。
3. **非美股代码要明说只支持美股**，并且**给退路**（留空不凑参照系）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webapp                                                # noqa: E402
from valuation import advisor as ad                          # noqa: E402


def _stat(metric="revenue_cagr", values=(0.10, 0.20, 0.30)):
    return ad.PeerStat(metric="近三年收入 CAGR", values=list(values),
                       labels=[f"T{i}" for i in range(len(values))],
                       source="SEC EDGAR（测试桩）", unit="")


class TestCompsEndpoint(unittest.TestCase):
    def test_empty_tickers_is_rejected_with_a_hint(self):
        out = webapp.api_comps({})
        self.assertFalse(out["ok"])
        self.assertIn("美股", out["error"])

    def test_unknown_metric_is_rejected(self):
        out = webapp.api_comps({"tickers": "LEA", "metric": "EV/EBITDA"})
        self.assertFalse(out["ok"])
        self.assertIn("不支持的指标", out["error"])

    def test_only_us_tickers_with_reason_and_way_out(self):
        with mock.patch("datasources.sec_edgar.ticker_to_cik", return_value=None):
            out = webapp.api_comps({"tickers": "600519, 0700"})
        self.assertFalse(out["ok"])
        self.assertIn("只支持美股", out["error"])
        self.assertIn("不凑一个参照系", out["error"])

    def test_returns_distribution_and_flags_small_sample(self):
        with mock.patch("datasources.sec_edgar.ticker_to_cik", return_value="0000001"), \
             mock.patch("valuation.advisor.build_peer_stat",
                        return_value=_stat(values=(0.10, 0.20))):
            out = webapp.api_comps({"tickers": "LEA, MGA"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["n"], 2)
        self.assertFalse(out["enough"], "样本 2 家必须判为不足 3 家")
        self.assertIn("min_comps", out)

    def test_enough_sample_is_flagged_ok(self):
        with mock.patch("datasources.sec_edgar.ticker_to_cik", return_value="0000001"), \
             mock.patch("valuation.advisor.build_peer_stat",
                        return_value=_stat()):
            out = webapp.api_comps({"tickers": "LEA, MGA, BWA"})
        self.assertTrue(out["enough"])
        self.assertEqual(out["n"], 3)
        self.assertIsNotNone(out["median"])

    def test_never_pretends_to_give_multiples(self):
        """★ EDGAR 没有股价。返回里出现市值/EV/倍数就是骗人。"""
        with mock.patch("datasources.sec_edgar.ticker_to_cik", return_value="0000001"), \
             mock.patch("valuation.advisor.build_peer_stat", return_value=_stat()):
            out = webapp.api_comps({"tickers": "LEA, MGA, BWA"})
        blob = " ".join(str(k) for k in out).lower()
        for forbidden in ("marketcap", "market_cap", "ev", "multiple", "enterprise"):
            self.assertNotIn(forbidden, blob, f"返回里出现了 {forbidden} —— 这一步给不了")

    def test_ticker_separators_are_forgiving(self):
        seen: list[list[tuple[str, str]]] = []

        def fake_pairs(pairs, **kw):
            seen.append(pairs)
            return _stat()

        with mock.patch("datasources.sec_edgar.ticker_to_cik", return_value="0000001"), \
             mock.patch("valuation.advisor.build_peer_stat", side_effect=fake_pairs):
            webapp.api_comps({"tickers": "LEA，MGA；BWA\nDAN"})
        self.assertEqual(len(seen[0]), 4)


if __name__ == "__main__":
    unittest.main()
