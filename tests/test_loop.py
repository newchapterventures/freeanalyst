"""编排循环的测试。

**把 `_chat` 换成假的** —— 测的是循环本身（挡重复、问用户、记轨迹），
不是某个模型的能力。模型能力那一层由 `bench/model_choice.py` 负责。

★ 盯得最紧的是两件事：

    **轨迹里要分清「谁定的」** —— 用户明确要求：
    「否则回头看会分不清哪些判断是你的」
    **重复调用不真的再跑一遍** —— 扫描很贵，而模型特别爱重复调
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import loop  # noqa: E402
from tools import registry as R  # noqa: E402


class FakeModel:
    """按剧本依次返回 `tool_calls`；剧本走完就返回纯文字（= 说做完了）。"""

    def __init__(self, script: list[tuple[str, dict]]):
        self.script = list(script)
        self.calls = []          # 每次被问到时记下来，便于断言喂回去了什么

    def __call__(self, model, messages, tools, timeout=300):
        self.calls.append(list(messages))
        if self.script:
            name, args = self.script.pop(0)
            return {"message": {"content": "",
                                "tool_calls": [{"function": {"name": name,
                                                              "arguments": args}}]}}
        return {"message": {"content": "做完了。", "tool_calls": []}}


class TempTools(unittest.TestCase):
    """注册表是全局的 —— 每一项用完都要还原。"""

    def setUp(self):
        self._saved = dict(R.REGISTRY)
        R.REGISTRY.clear()
        self.ran: list = []

        @R.tool("probe", summary="探一下", when="要探的时候", when_not="不用探的时候",
                parameters={"type": "object",
                            "properties": {"x": {"type": "string"}}},
                cost="很快")
        def probe(x: str = "") -> str:
            self.ran.append(x)
            return f"probe:{x}"

        @R.tool("boom", summary="会炸", when="w", when_not="n",
                parameters={"type": "object", "properties": {}})
        def boom() -> str:
            raise ValueError("炸了")

        self._orig_chat = loop._chat

    def tearDown(self):
        loop._chat = self._orig_chat
        R.REGISTRY.clear()
        R.REGISTRY.update(self._saved)


class TestLoop(TempTools):
    def test_does_two_steps_then_done(self):
        loop._chat = FakeModel([("probe", {"x": "a"}), ("probe", {"x": "b"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=5)
        self.assertEqual(tr.stop, loop.Stop.DONE)
        self.assertEqual([s.tool for s in tr.steps], ["probe", "probe"])
        self.assertEqual(self.ran, ["a", "b"])
        self.assertEqual(tr.final, "做完了。")

    def test_max_steps_stops_and_says_why(self):
        """★ 到上限要**说清为什么没走完**，不能默默停。"""
        loop._chat = FakeModel([("probe", {"x": str(i)}) for i in range(20)])
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3)
        self.assertEqual(tr.stop, loop.Stop.MAX_STEPS)
        self.assertEqual(len(tr.steps), 3)
        self.assertIn("3 步上限", tr.stuck)

    def test_duplicate_call_is_not_re_executed(self):
        """★ 模型特别爱重复调同一个工具。**不能真的再跑一遍** —— 扫描很贵。"""
        loop._chat = FakeModel([("probe", {"x": "same"}), ("probe", {"x": "same"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=5)
        self.assertEqual(self.ran, ["same"], "第二次不该真的执行")
        dup = tr.steps[1]
        self.assertFalse(dup.ok)
        self.assertIn("重复调用", dup.note)
        self.assertEqual(dup.seconds, 0.0)

    def test_different_args_is_not_a_duplicate(self):
        loop._chat = FakeModel([("probe", {"x": "a"}), ("probe", {"x": "b"})])
        loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=5)
        self.assertEqual(self.ran, ["a", "b"])

    def test_tool_error_does_not_stop_the_loop(self):
        """工具报错要**喂回给模型**，让它自己换动作 —— 这是编排能力本身。"""
        loop._chat = FakeModel([("boom", {}), ("probe", {"x": "ok"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=5)
        self.assertEqual(tr.stop, loop.Stop.DONE)
        self.assertFalse(tr.steps[0].ok)
        self.assertIn("炸了", tr.steps[0].brief)
        self.assertEqual(len(tr.steps), 2)

    def test_no_tools_registered_is_a_clear_error(self):
        R.REGISTRY.clear()
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK)
        self.assertEqual(tr.stop, loop.Stop.ERROR)
        self.assertIn("一个工具都没有", tr.stuck)


class TestWhoDecided(TempTools):
    """★ **轨迹里要分清「谁定的」。** 用户明确要求的。"""

    def test_one_click_marks_auto(self):
        loop._chat = FakeModel([("probe", {"x": "a"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3)
        self.assertEqual(tr.steps[0].by, loop.By.AUTO)

    def test_confirm_marks_user(self):
        loop._chat = FakeModel([("probe", {"x": "a"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=3,
                      on_ask=lambda k, p, q: (True, None))
        self.assertEqual(tr.steps[0].by, loop.By.USER)

    def test_render_shows_who(self):
        # ⚠️ 每次 run 都要**新的**假模型 —— 剧本是一次性的，复用会让第二次一步都不走
        loop._chat = FakeModel([("probe", {"x": "a"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=3,
                      on_ask=lambda k, p, q: (True, None))
        self.assertIn("你点头", tr.render())

        loop._chat = FakeModel([("probe", {"x": "a"})])
        tr2 = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3)
        self.assertIn("授权自决", tr2.render())


class TestAsking(TempTools):
    def test_refusing_a_step_stops_the_loop(self):
        loop._chat = FakeModel([("probe", {"x": "a"}), ("probe", {"x": "b"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=5,
                      on_ask=lambda k, p, q: (False, None))
        self.assertEqual(tr.stop, loop.Stop.REFUSED)
        self.assertEqual(self.ran, [], "拒了就不该执行")
        self.assertIn("拒", tr.stuck)

    def test_confirm_without_a_callback_just_runs(self):
        """没人可问的时候不能卡死 —— 按 one_click 走。"""
        loop._chat = FakeModel([("probe", {"x": "a"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=3,
                      on_ask=None)
        self.assertEqual(len(tr.steps), 1)

    def test_consent_gate_pauses_and_asks(self):
        """出网工具缺授权 → 交给 `on_ask`，**不是当失败**。"""
        from net import HostConsent

        @R.tool("net_tool", summary="会出网", when="w", when_not="n",
                parameters={"type": "object", "properties": {}},
                side_effect=R.SideEffect.EGRESS, hosts=("qt.gtimg.cn",))
        def net_tool() -> str:
            return "取到了"

        asked = []

        def ask(kind, payload, question):
            asked.append((kind, payload))
            if kind == "consent":
                return True, {"qt.gtimg.cn": HostConsent("qt.gtimg.cn", "测试")}
            return True, None

        loop._chat = FakeModel([("net_tool", {})])
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3,
                      on_ask=ask)
        self.assertEqual(tr.stop, loop.Stop.DONE)
        self.assertTrue(tr.steps[0].ok)
        self.assertEqual(asked[0][0], "consent")
        self.assertIn("qt.gtimg.cn", asked[0][1]["hosts"])

    def test_refusing_consent_stops_with_a_clear_reason(self):
        @R.tool("net_tool", summary="会出网", when="w", when_not="n",
                parameters={"type": "object", "properties": {}},
                side_effect=R.SideEffect.EGRESS, hosts=("qt.gtimg.cn",))
        def net_tool() -> str:
            return "不该走到这"

        loop._chat = FakeModel([("net_tool", {})])
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3,
                      on_ask=lambda k, p, q: (False, None))
        self.assertEqual(tr.stop, loop.Stop.NEEDS_CONSENT)
        self.assertIn("qt.gtimg.cn", tr.stuck)


class TestParsing(unittest.TestCase):
    def test_args_as_json_string(self):
        resp = {"message": {"tool_calls": [
            {"function": {"name": "t", "arguments": '{"a": 1}'}}]}}
        self.assertEqual(loop._calls(resp), [("t", {"a": 1})])

    def test_args_already_a_dict(self):
        resp = {"message": {"tool_calls": [
            {"function": {"name": "t", "arguments": {"a": 1}}}]}}
        self.assertEqual(loop._calls(resp), [("t", {"a": 1})])

    def test_broken_args_json_does_not_crash(self):
        resp = {"message": {"tool_calls": [
            {"function": {"name": "t", "arguments": "{不是 json"}}]}}
        name, args = loop._calls(resp)[0]
        self.assertEqual(name, "t")
        self.assertIn("__坏参数__", args)

    def test_empty_args(self):
        resp = {"message": {"tool_calls": [
            {"function": {"name": "t", "arguments": ""}}]}}
        self.assertEqual(loop._calls(resp), [("t", {})])

    def test_key_is_order_insensitive(self):
        self.assertEqual(loop._key("t", {"a": 1, "b": 2}),
                         loop._key("t", {"b": 2, "a": 1}))


class TestTraceSave(TempTools):
    def test_save_json_round_trips(self):
        loop._chat = FakeModel([("probe", {"x": "a"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3)
        p = Path(tempfile.mkdtemp()) / "trace.json"
        tr.save_json(str(p))
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["goal"], "目标")
        self.assertEqual(d["steps"][0]["tool"], "probe")
        self.assertEqual(d["steps"][0]["by"], "auto")

    def test_model_error_is_recorded_not_raised(self):
        def bad(model, messages, tools, timeout=300):
            raise loop.LoopError("连不上")

        loop._chat = bad
        tr = loop.run("目标", autonomy=loop.Autonomy.ONE_CLICK)
        self.assertEqual(tr.stop, loop.Stop.ERROR)
        self.assertIn("连不上", tr.stuck)


class TestSessionPauseResume(TempTools):
    """★ **网页那边不能挂着等用户点按钮** —— 所以循环要能暂停、能恢复。

    命令行用 `run()`（同步回调一口气驱动完）；网页用 `Session` 自己驱动。
    """

    def test_confirm_mode_pauses_before_each_step(self):
        loop._chat = FakeModel([("probe", {"x": "a"})])
        s = loop.Session("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=3)
        out = s.advance()
        self.assertIsInstance(out, loop.Ask)
        self.assertEqual(out.kind, "confirm")
        self.assertEqual(out.payload["tool"], "probe")
        self.assertEqual(self.ran, [], "还没点头就不该执行")

    def test_resume_with_yes_executes_and_continues(self):
        loop._chat = FakeModel([("probe", {"x": "a"})])
        s = loop.Session("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=3)
        s.advance()
        out = s.advance((True, None))
        self.assertEqual(self.ran, ["a"])
        self.assertIsInstance(out, loop.Trace)
        self.assertEqual(out.stop, loop.Stop.DONE)
        self.assertEqual(out.steps[0].by, loop.By.USER)

    def test_resume_with_no_stops(self):
        loop._chat = FakeModel([("probe", {"x": "a"}), ("probe", {"x": "b"})])
        s = loop.Session("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=5)
        s.advance()
        out = s.advance((False, None))
        self.assertIsInstance(out, loop.Trace)
        self.assertEqual(out.stop, loop.Stop.REFUSED)
        self.assertEqual(self.ran, [])
        self.assertIn("拒", out.stuck)

    def test_one_click_does_not_pause(self):
        loop._chat = FakeModel([("probe", {"x": "a"})])
        s = loop.Session("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3)
        out = s.advance()
        self.assertIsInstance(out, loop.Trace)
        self.assertEqual(out.steps[0].by, loop.By.AUTO)

    def test_consent_pauses_then_resumes(self):
        from net import HostConsent

        @R.tool("net_tool", summary="会出网", when="w", when_not="n",
                parameters={"type": "object", "properties": {}},
                side_effect=R.SideEffect.EGRESS, hosts=("qt.gtimg.cn",))
        def net_tool() -> str:
            return "取到了"

        loop._chat = FakeModel([("net_tool", {})])
        s = loop.Session("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3)
        out = s.advance()
        self.assertIsInstance(out, loop.Ask)
        self.assertEqual(out.kind, "consent")
        self.assertIn("qt.gtimg.cn", out.payload["hosts"])

        out = s.advance((True, {"qt.gtimg.cn": HostConsent("qt.gtimg.cn", "测试")}))
        self.assertIsInstance(out, loop.Trace)
        self.assertTrue(out.steps[0].ok)

    def test_refusing_consent_stops_cleanly(self):
        @R.tool("net_tool", summary="会出网", when="w", when_not="n",
                parameters={"type": "object", "properties": {}},
                side_effect=R.SideEffect.EGRESS, hosts=("qt.gtimg.cn",))
        def net_tool() -> str:
            return "不该走到这"

        loop._chat = FakeModel([("net_tool", {})])
        s = loop.Session("目标", autonomy=loop.Autonomy.ONE_CLICK, max_steps=3)
        s.advance()
        out = s.advance((False, None))
        self.assertEqual(out.stop, loop.Stop.NEEDS_CONSENT)
        self.assertIn("qt.gtimg.cn", out.stuck)

    def test_ask_is_serialisable(self):
        """网页要把这个问题原样交给前端，所以得能变 dict。"""
        loop._chat = FakeModel([("probe", {"x": "a"})])
        s = loop.Session("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=3)
        d = s.advance().as_dict()
        self.assertEqual(set(d), {"kind", "payload", "question"})
        json.dumps(d, ensure_ascii=False)

    def test_run_without_callback_treats_confirm_as_yes(self):
        """命令行没给回调时不能卡死 —— 当成同意往下走。"""
        loop._chat = FakeModel([("probe", {"x": "a"})])
        tr = loop.run("目标", autonomy=loop.Autonomy.CONFIRM, max_steps=3,
                      on_ask=None)
        self.assertEqual(len(tr.steps), 1)
        self.assertEqual(self.ran, ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
