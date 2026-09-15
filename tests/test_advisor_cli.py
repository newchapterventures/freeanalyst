"""假设参谋 CLI 测试。

重点在**失败时不能静默**：

- 同行取数失败 → 报告里要写出来，同时仍然给出证据清单
- 参谋整个挂掉 → `value.py` 里要显示「跳过」和原因，
  不能让人以为这一节本来就没有

**静默跳过比报错更危险** —— 用户会以为估值报告是完整的。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from valuation import advisor as ad  # noqa: E402
from valuation import advisor_cli as cli  # noqa: E402
from valuation.core import Confidence  # noqa: E402


class TestAssumptionParsing(unittest.TestCase):
    def test_object_with_source(self):
        a = cli._assumption(
            {"value": 0.146, "source": "管理层规划", "confidence": "低"}, "增长")
        self.assertEqual(a.value, 0.146)
        self.assertEqual(a.source, "管理层规划")
        self.assertIs(a.confidence, Confidence.LOW)

    def test_bare_number_is_low_confidence(self):
        a = cli._assumption(0.146, "增长")
        self.assertIs(a.confidence, Confidence.LOW)
        self.assertIn("裸数字", a.source)

    def test_missing_value_raises_with_position(self):
        with self.assertRaises(cli.AdvisorConfigError) as cm:
            cli._assumption({"source": "x"}, "预测期收入增长率")
        self.assertIn("预测期收入增长率", str(cm.exception))
        self.assertIn("value", str(cm.exception))


class TestPeerSetConfig(unittest.TestCase):
    def test_missing_tickers_raises(self):
        with self.assertRaises(cli.AdvisorConfigError) as cm:
            cli._build_peer_sets({"peer_sets": {"汽车": {}}})
        self.assertIn("tickers", str(cm.exception))

    def test_unknown_ticker_raises_with_the_code(self):
        """写了非美股代码要明确说不支持，而不是静默少一家。"""
        if ad.se.ticker_to_cik("600519") is not None:
            self.skipTest("600519 竟然在 SEC 里能查到，这个用例不成立")
        with self.assertRaises(cli.AdvisorConfigError) as cm:
            cli._build_peer_sets({"peer_sets": {"A股": {"tickers": ["600519"]}}})
        msg = str(cm.exception)
        self.assertIn("600519", msg)
        self.assertIn("只支持美股", msg)
        # 还要告诉用户「留空」是可以的
        self.assertIn("不凑一个参照系", msg)

    def test_empty_peer_sets_ok(self):
        self.assertEqual(cli._build_peer_sets({}), {})
        self.assertEqual(cli._build_peer_sets({"peer_sets": None}), {})


class TestRunAdvisor(unittest.TestCase):
    """不联网的路径 —— 用 peer_stat 直接喂，或者干脆不给 peer_set。"""

    def _cfg(self, **over):
        base = {
            "advisor": {
                "review": [
                    {"name": "2027 年收入增长", "value": 0.146,
                     "source": "管理层规划", "confidence": "低"},
                ],
            }
        }
        base["advisor"].update(over)
        return base

    def test_no_advisor_section_is_a_noop(self):
        out: list[str] = []
        cli.run_advisor({}, out)
        self.assertEqual(out, [])

    def test_review_without_peer_set_gives_evidence_only(self):
        out: list[str] = []
        cli.run_advisor(self._cfg(), out)
        text = "\n".join(out)
        self.assertIn("假设参谋", text)
        self.assertIn("2027 年收入增长", text)
        self.assertIn("不给你凑一个参照系", text)
        self.assertIn("已签合同/在手订单", text)

    def test_missing_peer_set_reference_is_flagged(self):
        cfg = self._cfg()
        cfg["advisor"]["review"][0]["peer_set"] = "不存在的集合"
        out: list[str] = []
        cli.run_advisor(cfg, out)
        self.assertIn("找不到 peer_set", "\n".join(out))

    def test_empty_review_says_so(self):
        out: list[str] = []
        cli.run_advisor({"advisor": {"review": []}}, out)
        self.assertIn("没有配置要复核的假设", "\n".join(out))

    def test_item_without_name_raises(self):
        with self.assertRaises(cli.AdvisorConfigError):
            cli.run_advisor({"advisor": {"review": [{"value": 0.1}]}}, [])


class TestTerminalGrowthSection(unittest.TestCase):
    def _cfg(self, g, gdp, infl=None):
        tg = {"g": {"value": g, "source": "管理层"},
              "long_term_nominal_gdp": {"value": gdp, "source": "社科院"}}
        if infl is not None:
            tg["long_term_inflation"] = {"value": infl, "source": "央行"}
        return {"advisor": {"review": [], "terminal_growth": tg}}

    def test_passing_case(self):
        out: list[str] = []
        cli.run_advisor(self._cfg(0.025, 0.045, 0.02), out)
        text = "\n".join(out)
        self.assertIn("永续增长的硬边界检查", text)
        self.assertIn("✓ 通过", text)

    def test_exceeding_gdp_is_flagged(self):
        out: list[str] = []
        cli.run_advisor(self._cfg(0.06, 0.045, 0.02), out)
        self.assertIn("数学上不成立", "\n".join(out))

    def test_inflation_is_optional(self):
        out: list[str] = []
        cli.run_advisor(self._cfg(0.025, 0.045), out)
        self.assertIn("✓ 通过", "\n".join(out))


class TestValuePyIntegration(unittest.TestCase):
    """`value.py` 里的参谋失败必须显式说出来。"""

    def test_value_py_wraps_advisor_in_try(self):
        src = (ROOT / "value.py").read_text(encoding="utf-8")
        self.assertIn("run_advisor", src)
        self.assertIn("不静默跳过", src)

    def test_demo_config_has_advisor_section(self):
        import json
        cfg = json.loads((ROOT / "examples" / "valuation-demo.json")
                         .read_text(encoding="utf-8"))
        self.assertIn("advisor", cfg)
        self.assertTrue(cfg["advisor"]["review"])


if __name__ == "__main__":
    unittest.main()
