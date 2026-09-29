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


class TestCompsMarketConsistency(unittest.TestCase):
    """★ 用户提的专业点：**不同股市的 beta 与估值中枢本来就不一样。**

    所以"标的在 A 股 / 港股，同行取的是美股"这种情况，工具**必须说清后果** ——
    不是加一句"仅供参考"的软话，而是说清输出**不能**怎么用，再给可走的路。

    币种从**报表单位**里读（"千美元" / "人民币千元"）—— 这是材料自己写的口径，
    读它是"用它自己的话说它自己"，不是猜。
    """

    def test_currency_is_read_from_the_unit_string(self):
        self.assertEqual(webapp._market_of("千美元"), "us")
        self.assertEqual(webapp._market_of("百万美元"), "us")
        self.assertEqual(webapp._market_of("人民币千元"), "cn")
        self.assertEqual(webapp._market_of("千元"), "cn")
        self.assertEqual(webapp._market_of("千港元"), "hk")

    def test_unreadable_unit_does_not_trigger_a_warning(self):
        """判不出市场时**不警告** —— 不许误报（会喊狼嚎的提示比没有更坏）。"""
        self.assertEqual(webapp._comps_market_warning("", "us"), "")
        self.assertEqual(webapp._market_of(""), "")

    def test_renminbi_units_count_as_cn(self):
        """「元 / 万元」= 人民币口径 → 拿美股同行比就该警告。"""
        for unit in ("元", "万元"):
            self.assertEqual(webapp._market_of(unit), "cn")
            self.assertTrue(webapp._comps_market_warning(unit, "us"))

    def test_same_market_has_no_warning(self):
        self.assertEqual(webapp._comps_market_warning("千美元", "us"), "")
        self.assertEqual(webapp._comps_market_warning("人民币千元", "cn"), "")

    def test_cross_market_warning_says_what_not_to_do(self):
        w = webapp._comps_market_warning("人民币千元", "us")
        self.assertIn("口径不一致", w)
        self.assertIn("beta", w)
        self.assertIn("估值中枢", w)
        self.assertIn("不能", w.replace("不是", "不能"))     # 说清"不能用在哪"
        self.assertIn("同市场", w)                            # 给可走的路

    def test_endpoint_reports_the_target_market(self):
        with mock.patch("datasources.sec_edgar.ticker_to_cik", return_value="0000001"), \
             mock.patch("valuation.advisor.build_peer_stat", return_value=_stat()):
            out = webapp.api_comps({"tickers": "LEA", "target_unit": "人民币千元"})
        self.assertEqual(out["peer_market"], "us")
        self.assertEqual(out["target_market"], "cn")
        self.assertTrue(out["market_warning"], "跨市场却没警告")


class TestMarketScopeLayer(unittest.TestCase):
    """市场层（`_comps_scope`）—— "覆盖中/美/港"的地基。

    跨市场对照**必须分级**：基本面比率可以比，绝对规模要同币种，
    倍数与 beta **必须同市场 + 同币种**。
    """

    def test_same_market_and_currency_permits_everything(self):
        s = webapp._comps_scope("us", "千美元", "us", "USD", "USD")
        self.assertTrue(s["same_market"])
        self.assertTrue(s["multiples_allowed"])
        self.assertTrue(s["scale_allowed"])

    def test_cross_market_blocks_multiples_but_not_ratios(self):
        s = webapp._comps_scope("", "人民币千元", "us", "USD", "CNY")
        self.assertFalse(s["same_market"])
        self.assertFalse(s["multiples_allowed"], "跨市场竟然允许套倍数")
        self.assertTrue(s["ratios_allowed"], "跨市场连比率都不给比，太紧")
        self.assertFalse(s["scale_allowed"], "跨币种竟然允许比绝对规模")

    def test_currency_is_inferred_when_only_the_unit_is_known(self):
        """★ 实测踩到的坑：页面只传单位（不传币种）时，
        `same_currency` 曾被算成 True，于是**跨币种的绝对规模也放行了** ✗。"""
        s = webapp._comps_scope("", "人民币千元", "us", "USD")
        self.assertEqual(s["target_currency"], "CNY")
        self.assertFalse(s["same_currency"])
        self.assertFalse(s["scale_allowed"])
        self.assertFalse(s["multiples_allowed"])

    def test_declaration_beats_currency_inference(self):
        """港股里人民币报表很常见 —— 所以**声明优先**，币种只是线索。"""
        s = webapp._comps_scope("hk", "人民币千元", "us", "USD", "HKD")
        self.assertEqual(s["target_market"], "hk")
        self.assertEqual(s["target_market_inferred"], "cn")
        self.assertTrue(any("按你的声明走" in n for n in s["notes"]),
                        "声明与推断不一致时必须说明")

    def test_unknown_market_is_not_assumed_to_match(self):
        """判不出市场时**不许假设同市场**（宁可少说，不可多说）。"""
        s = webapp._comps_scope("", "", "us", "USD", "")
        self.assertEqual(s["target_market"], "")
        self.assertFalse(s["same_market"])
        self.assertFalse(s["multiples_allowed"])
        self.assertTrue(s["notes"])

    def test_source_declares_its_market(self):
        """市场和币种必须由**源自己**声明 —— 接口里不许写死。"""
        from datasources import sec_edgar
        self.assertEqual(sec_edgar.MARKET, "us")
        self.assertEqual(sec_edgar.CURRENCY, "USD")
        import inspect
        src = inspect.getsource(webapp.api_comps)
        self.assertIn("se.MARKET", src)
        self.assertNotIn('peer_market = "us"', src)


class TestMultiplesGate(unittest.TestCase):
    """倍数闸 —— 报错要说**真话**。

    以前请求 `ev_ebitda` 会得到「不支持的指标」✗ —— 而真因是
    **这个源根本没有股价**（EDGAR 是申报系统）。两件事不一样：
    前者让人以为"再写个函数就行"，后者指向"要配一个价格源"。
    """

    def test_source_declares_it_has_no_prices(self):
        from datasources import sec_edgar
        self.assertIs(sec_edgar.HAS_PRICES, False)

    def test_a_multiple_without_a_declared_market_says_what_is_unknown(self):
        """市场没声明时「不能跨市场」这句话仍然成立 —— 它得说清**不知道什么**。"""
        out = webapp.api_comps({"tickers": "LEA,MGA", "metric": "ev_ebitda"})
        self.assertFalse(out["ok"])
        self.assertIn("不能跨市场", out["error"])
        self.assertIn("未声明", out["error"])
        self.assertNotIn("不支持的指标", out["error"], "又拿「不支持」糊弄人了")

    def test_cross_market_multiple_is_refused_for_the_market_reason(self):
        """源有行情、但跨市场 —— 理由应该是**市场**，不是缺价。"""
        from datasources import sec_edgar
        with mock.patch.object(sec_edgar, "HAS_PRICES", True):
            out = webapp.api_comps({"tickers": "LEA,MGA", "metric": "ev_ebitda",
                                    "target_unit": "人民币千元"})
        self.assertFalse(out["ok"])
        self.assertIn("跨市场", out["error"])
        self.assertIn("同市场", out["error"])

    def test_ev_multiple_computes_with_net_debt(self):
        """EV 类已经能算了 —— 前提是**净债务取得到**；取不到就记缺口。"""
        out = webapp.api_comps({"tickers": "LEA,MGA", "metric": "ev_ebitda",
                                "target_market": "us", "target_currency": "USD",
                                "as_of": "2025-06-30"})
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["title"], "EV / EBITDA")
        # 无论样本够不够，都**不许**静默把某一家丢掉：
        # 每家要么在 rows 里，要么在 gaps 里
        self.assertEqual(len(out["rows"]) + len(out["gaps"]), 2)
        for b in out["basis"]:
            self.assertIn("净债务", b)
            self.assertIn("口径", b)


if __name__ == "__main__":
    unittest.main()
