"""早期项目 CLI 测试。

重点在两个地方：

**① 锚点缺失必须报错，不能有默认值。**
   `per_factor_cap` 和 Scorecard 的 `base` 是外部锚点，不是从标的数据
   算出来的。给它们一个"合理默认值"是最糟的设计 —— 用户会拿到一个
   看起来正常的数，而它其实来自别人的市场、别人的年份。

**② 配置写错要指出位置。**
   用户是自然人，不是程序员。报错必须说清是哪个字段写错了、为什么。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import early_cli  # noqa: E402
from valuation.core import Confidence  # noqa: E402

DEMO = ROOT / "examples" / "early-stage-demo.json"


class TestRangeParsing(unittest.TestCase):
    def test_plain_number(self):
        r = early_cli._range(300, "x")
        self.assertEqual((r.low, r.mid, r.high), (300.0, 300.0, 300.0))

    def test_mid_object(self):
        r = early_cli._range({"mid": 100, "low": 80, "high": 120}, "x")
        self.assertEqual((r.low, r.mid, r.high), (80.0, 100.0, 120.0))

    def test_mid_only_defaults_low_high_to_mid(self):
        r = early_cli._range({"mid": 100}, "x")
        self.assertEqual((r.low, r.high), (100.0, 100.0))

    def test_three_element_list(self):
        r = early_cli._range([80, 100, 120], "x")
        self.assertEqual((r.low, r.mid, r.high), (80.0, 100.0, 120.0))

    def test_bad_list_length_raises(self):
        with self.assertRaises(early_cli.ConfigError) as cm:
            early_cli._range([80, 100], "x")
        self.assertIn("三个数", str(cm.exception))

    def test_none_raises(self):
        with self.assertRaises(early_cli.ConfigError):
            early_cli._range(None, "x")


class TestAssumptionParsing(unittest.TestCase):
    def test_bare_number_is_low_confidence(self):
        """没有来源的假设就是低置信度的假设，不该被当成事实。"""
        a = early_cli._assumption(100, "x")
        self.assertIs(a.confidence, Confidence.LOW)
        self.assertIn("裸数字", a.source)

    def test_object_carries_source(self):
        a = early_cli._assumption(
            {"value": 100, "source": "TS 条款", "confidence": "高"}, "x")
        self.assertEqual(a.source, "TS 条款")
        self.assertIs(a.confidence, Confidence.HIGH)

    def test_object_without_value_raises(self):
        with self.assertRaises(early_cli.ConfigError) as cm:
            early_cli._assumption({"source": "x"}, "退出价值")
        self.assertIn("退出价值", str(cm.exception))
        self.assertIn("value", str(cm.exception))


class TestMissingAnchorsAreRejected(unittest.TestCase):
    """锚点必须显式给 —— 这是整个模块最重要的设计决定。"""

    def test_berkus_cap_is_required(self):
        with self.assertRaises(early_cli.ConfigError) as cm:
            early_cli._build_berkus(
                {"berkus": {"factors": {"基本价值": 0.5}}}, "万元")
        self.assertIn("per_factor_cap", str(cm.exception))
        self.assertIn("没有默认值", str(cm.exception))

    def test_scorecard_base_is_required(self):
        with self.assertRaises(early_cli.ConfigError) as cm:
            early_cli._build_scorecard(
                {"scorecard": {"factors": {"管理团队实力": 1.1}}}, "万元")
        self.assertIn("base", str(cm.exception))
        self.assertIn("外部事实", str(cm.exception))

    def test_vc_needs_a_target(self):
        with self.assertRaises(early_cli.ConfigError) as cm:
            early_cli._build_vc({"vc_method": {
                "exit_value": 1000, "investment": 100, "years_to_exit": 5,
            }}, "万元")
        self.assertIn("target_multiple", str(cm.exception))

    def test_reverse_without_ask_price_raises(self):
        with self.assertRaises(early_cli.ConfigError) as cm:
            early_cli._build_vc({"vc_method": {
                "exit_value": 1000, "investment": 100, "years_to_exit": 5,
                "target_multiple": 10, "reverse": True,
            }}, "万元")
        self.assertIn("ask_price", str(cm.exception))

    def test_empty_berkus_factors_raises(self):
        with self.assertRaises(early_cli.ConfigError):
            early_cli._build_berkus({"berkus": {"per_factor_cap": 300, "factors": {}}}, "万元")


class TestEndToEnd(unittest.TestCase):
    """跑完整流程。用 redirect_stdout 收住输出，别污染测试报告。"""

    @staticmethod
    def _run(cfg, show_trace=False):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = early_cli.run_early_stage(cfg, show_trace=show_trace)
        return code, buf.getvalue()

    def test_demo_config_runs(self):
        cfg = json.loads(DEMO.read_text(encoding="utf-8"))
        code, out = self._run(cfg)
        self.assertEqual(code, 0)
        self.assertIn("早期项目估值", out)
        self.assertIn("多方法对照", out)

    def test_demo_has_all_four_methods(self):
        cfg = json.loads(DEMO.read_text(encoding="utf-8"))
        es_cfg = cfg["early_stage"]
        for key in ("berkus", "scorecard", "vc_method", "first_chicago"):
            self.assertIn(key, es_cfg, f"演示配置缺 {key}")

    def test_reverse_result_shown_but_not_in_spread(self):
        cfg = json.loads(DEMO.read_text(encoding="utf-8"))
        _, out = self._run(cfg)
        spread = [l for l in out.splitlines() if "中枢差额" in l][0]
        self.assertNotIn("242,857", spread)
        self.assertIn("不属于估值", out)

    def test_empty_early_stage_returns_error(self):
        code, out = self._run({"early_stage": {}})
        self.assertEqual(code, 1)
        self.assertIn("至少要有一个", out)

    def test_bad_config_returns_error_not_traceback(self):
        """配置写错要给出可读的错误，不是 Python 堆栈。"""
        code, out = self._run({"early_stage": {"berkus": {"factors": {"基本价值": 0.5}}}})
        self.assertEqual(code, 1)
        self.assertIn("per_factor_cap", out)
        self.assertNotIn("Traceback", out)


class TestValuePyDispatch(unittest.TestCase):
    """`value.py` 看到 early_stage 段就自动分流，不用记两个命令。"""

    def test_dispatch_detects_early_stage_key(self):
        src = (ROOT / "value.py").read_text(encoding="utf-8")
        self.assertIn('if "early_stage" in cfg:', src)
        self.assertIn("run_early_stage", src)

    def test_regular_config_still_has_no_early_stage_key(self):
        """现有那个带三张表的示例配置不能被误分流。"""
        other = ROOT / "examples" / "valuation-demo.json"
        if other.exists():
            cfg = json.loads(other.read_text(encoding="utf-8"))
            self.assertNotIn("early_stage", cfg)


if __name__ == "__main__":
    unittest.main()
