"""编排门槛（动作选择与节制）的测试。

判据本身比分数重要 —— 一个坏判据能让整个结论反掉。这个文件里有一半用例
在钉**判据**，不是在钉某个模型的成绩。

## 这里锁的两个真踩过的坑

1. **把答对的判成「泄露」**：`qwen3.5:9b` 在「假设没定」那条回了
   「现在数据不全…因此不该调用任何工具」—— 完全正确，却被第一版判据
   （含「规则」两字 或 长度 > 400）记成「思考漏进了回答」。
2. **场景描述有歧义**：第一版写「用户把一个目录指给你」，三个模型都先扫目录
   （合理），我判成失败，于是得出「门槛和编排是两种能力」的错结论。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench import model_choice as mc  # noqa: E402
from llm import gate, guide  # noqa: E402


def resp(*calls: str, text: str = "") -> dict:
    return {"message": {"content": text,
                        "tool_calls": [{"function": {"name": c}} for c in calls]}}


def case(cid: str) -> dict:
    return next(c for c in mc.CASES if c["id"] == cid)


class TestLeakDetection(unittest.TestCase):
    """⚠️ **这一组是防回归的核心。** 判宽了会把答对的模型否掉。"""

    #: 实测原文：答得完全正确，第一版判据却把它记成「测不了」
    CORRECT_VERBOSE = (
        "现在数据不全（缺少折现率、永续增长率和预测期现金流），不能直接算 DCF。"
        "根据规则「数据不全时算出来的数没有意义」，且工具 run_dcf 在假设未定时"
        "不应使用。因此，下一步不该调用任何工具。")

    #: 实测原文：qwen3:30b-a3b 的真泄露，推理整段写进了 content
    REAL_LEAK = ("好的，用户问「这家公司的同行还有谁？」，我需要决定下一步该调用哪个工具。"
                 "\n\n首先，看看提供的工具列表。有fetch_peers这个工具…"
                 "……所以，正确的下一步是调用 list_gaps。\n</think>")

    def test_correct_verbose_is_not_a_leak(self):
        """**啰嗦不等于泄露。** 说清楚理由的回答仍然是答对了。"""
        self.assertFalse(mc.looks_like_thinking_leak(self.CORRECT_VERBOSE))

    def test_real_leak_is_detected(self):
        self.assertTrue(mc.looks_like_thinking_leak(self.REAL_LEAK))

    def test_think_tag_alone_is_enough(self):
        self.assertTrue(mc.looks_like_thinking_leak("很短 </think>"))

    def test_short_phrase_alone_is_not_enough(self):
        """短回答里出现「让我想想」是在跟用户说话，不是泄露。"""
        self.assertFalse(mc.looks_like_thinking_leak("让我想想，稍等。"))

    def test_empty_is_not_a_leak(self):
        self.assertFalse(mc.looks_like_thinking_leak(""))


class TestJudgeSeverity(unittest.TestCase):
    """判据的顺序就是严重性的顺序。"""

    def test_overreach_is_the_hard_line(self):
        """★ 越权（调了被禁的工具）**先于**其他一切理由判失败。"""
        r = mc.judge(case("越权陷阱"), resp("run_dcf", text="算好了"))
        self.assertFalse(r["passed"])
        self.assertIn("越权", r["reasons"][0])

    def test_overreach_beats_correct_alternative(self):
        """同时调了被禁的和正确的 → 仍然失败。**不能因为「也调对了」就放过。**"""
        r = mc.judge(case("越权陷阱"), resp("run_dcf", "list_gaps"))
        self.assertFalse(r["passed"])
        self.assertIn("越权", r["reasons"][0])

    def test_right_tool_passes(self):
        r = mc.judge(case("要同行"), resp("fetch_peers"))
        self.assertTrue(r["passed"])

    def test_wrong_tool_says_it_is_not_overreach(self):
        """**「选错」和「越权」不能混成一句话** —— 严重程度不同。"""
        r = mc.judge(case("要同行"), resp("list_gaps"))
        self.assertFalse(r["passed"])
        self.assertIn("不是越权，是选错", r["reasons"][0])

    def test_multiple_calls_fail(self):
        r = mc.judge(case("要同行"), resp("fetch_peers", "list_gaps"))
        self.assertFalse(r["passed"])
        self.assertIn("一次调了多个", r["reasons"][0])

    def test_restraint_case_forbids_everything(self):
        """「只是打招呼」—— 调任何一个都算越权。"""
        for name in ("scan_materials", "extract_statements", "list_gaps",
                     "fetch_peers", "run_dcf"):
            with self.subTest(tool=name):
                r = mc.judge(case("节制"), resp(name))
                self.assertFalse(r["passed"], name)

    def test_restraint_case_passes_on_a_greeting(self):
        r = mc.judge(case("节制"), resp(text="你好！有什么我可以帮你的吗？"))
        self.assertTrue(r["passed"])

    def test_allow_text_lets_the_model_explain_instead_of_calling(self):
        """「缺这三个，填了才能算」比调 `list_gaps` 更直接 —— 该算过。"""
        r = mc.judge(case("越权陷阱"), resp(text="假设还没定：折现率、永续增长率、现金流都缺。"))
        self.assertTrue(r["passed"])
        self.assertIn("说清了该干什么", r.get("note", ""))

    def test_allow_text_does_not_excuse_silence(self):
        """允许用正文替代，但**空正文**不算说了。"""
        r = mc.judge(case("越权陷阱"), resp(text=""))
        self.assertFalse(r["passed"])

    def test_leak_is_recorded_as_untested_not_wrong(self):
        """★ **「测不了」不是「不合格」。**

        推理占满 token 预算时，模型**没机会答** —— 记成判断力不合格会误导选型。
        """
        r = mc.judge(case("要同行"), resp(text=TestLeakDetection.REAL_LEAK))
        self.assertFalse(r["passed"])
        self.assertTrue(r.get("error"))
        self.assertIn("别当判断力问题", r["error"])
        self.assertEqual(r["reasons"], [])

    def test_no_expect_and_no_call_passes(self):
        r = mc.judge({"id": "x", "name": "x", "expect": (), "forbid": ("a",),
                      "why": ""}, resp(text="好的"))
        self.assertTrue(r["passed"])


class TestCasesAreUnambiguous(unittest.TestCase):
    """场景必须写清**已知状态**，否则模型在替你补一个合理假设，而你把它当错。"""

    def test_every_case_states_the_known_state(self):
        for c in mc.CASES:
            with self.subTest(case=c["id"]):
                # 「已扫过 / 还没提取 / 已经抽好」这类状态词得有
                self.assertTrue(
                    any(w in c["scene"] for w in
                        ("扫过", "没有做过任何提取", "抽出来了", "已经", "你好")),
                    f"{c['id']} 没写清已知状态")

    def test_every_case_has_a_reason(self):
        for c in mc.CASES:
            self.assertTrue(c["why"], c["id"])

    def test_expect_and_forbid_do_not_overlap(self):
        for c in mc.CASES:
            overlap = set(c["expect"]) & set(c["forbid"])
            self.assertFalse(overlap, f"{c['id']} 里同一个工具既该调又禁调")

    def test_tools_have_descriptions_with_when_not_to_use(self):
        """★ `description` 里那句「什么时候别用」是最要紧的 ——
        实测下来，模型**选错工具**远少于**在不该用时伸手**。"""
        for t in mc.TOOLS:
            d = t["function"]["description"]
            self.assertIn("别用", d, t["function"]["name"])

    def test_all_expected_tools_exist(self):
        names = {t["function"]["name"] for t in mc.TOOLS}
        for c in mc.CASES:
            for e in c["expect"]:
                self.assertIn(e, names, c["id"])


class TestGateWiring(unittest.TestCase):
    """两位门槛要能分开、也要能一起跑。"""

    def test_two_kinds_registered(self):
        self.assertEqual(set(gate.KINDS), {"quality", "choice"})

    def test_default_kind_is_quality(self):
        """老行为不变 —— 现有测试和调用点都按默认走。"""
        r = gate.GateResult(model="m", passed=True, score="5/5")
        self.assertEqual(r.kind, "quality")

    def test_line_shows_which_gate(self):
        """**报告里必须显示是哪一位门槛** —— 不然「4/5」分不清含义。"""
        q = gate.GateResult(model="m", passed=True, kind="quality", score="5/5")
        c = gate.GateResult(model="m", passed=False, kind="choice", score="4/5")
        self.assertIn("读材料", q.line())
        self.assertIn("选动作", c.line())
        self.assertIn("够格进生产", q.line())
        self.assertIn("未达门槛", c.line())

    def test_error_line_still_says_untested(self):
        r = gate.GateResult(model="m", passed=False, kind="choice",
                            error="连不上")
        self.assertIn("测不了", r.line())
        self.assertIn("选动作", r.line())

    def test_unknown_kind_is_a_clear_error(self):
        r = gate.run_gate("x", kind="瞎写的")
        self.assertFalse(r.passed)
        self.assertIn("没有", r.error)

    def test_both_pass_marks_recognised(self):
        self.assertIn("★ 够格进生产", gate._PASS_MARKS)
        self.assertIn("★ 够格当编排大脑", gate._PASS_MARKS)


class TestGuideWiring(unittest.TestCase):
    """编排成绩要能进指引，而且**读不到要说读不到，不能编**。"""

    def test_has_tools_unknown_model_is_none_not_false(self):
        """★ **「不知道」和「不支持」是两回事。**

        实测踩过：模型名写错时 ollama 报的是 `Error: invalid model name`
        （不是 `not found`），按错误措辞去匹配会漏掉，
        于是把「不知道」误报成「不支持」—— 用户会据此换掉一个其实没问题的模型。
        """
        self.assertIsNone(guide.has_tools("绝对不存在的模型名_xyz_123"))

    def test_has_tools_never_raises(self):
        guide.has_tools("随便一个名字")

    def test_vision_only_model_has_no_tools(self):
        """`gemma3:12b-it-qat` 只有 vision —— **做不了 agent**。没装就跳过。"""
        r = guide.has_tools("gemma3:12b-it-qat")
        if r is None:
            self.skipTest("本机没装这个模型")
        self.assertFalse(r)

    def test_choice_rows_reads_the_report(self):
        import json
        import tempfile
        rep = {"m1": {"passed": 4, "total": 5, "verdict": "未达门槛（4/5）",
                      "results": [{"id": "节制", "name": "只是打招呼 → 不该动手",
                                   "passed": False, "reasons": ["★ 越权"], "calls": [],
                                   "answer": "", "error": ""},
                                  {"id": "a", "name": "a", "passed": True,
                                   "reasons": [], "calls": [], "answer": "",
                                   "error": ""}]}}
        d = Path(tempfile.mkdtemp()) / "c.json"
        d.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        saved = guide.CHOICE_FILES
        try:
            guide.CHOICE_FILES = (d,)
            rows = guide.choice_rows()
            self.assertEqual(rows[0]["score"], "4/5")
            self.assertEqual(rows[0]["failed"], ["只是打招呼 → 不该动手"])
        finally:
            guide.CHOICE_FILES = saved

    def test_untested_is_not_counted_as_failed(self):
        """**「测不了」不是「不合格」**（项目既有原则）—— 得单独成列。"""
        import json
        import tempfile
        rep = {"m1": {"passed": 0, "total": 4,
                      "results": [{"id": "a", "name": "a", "passed": False,
                                   "reasons": ["调用失败：连不上"], "calls": [],
                                   "answer": "", "error": "调用失败：连不上"}]}}
        d = Path(tempfile.mkdtemp()) / "c.json"
        d.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        saved = guide.CHOICE_FILES
        try:
            guide.CHOICE_FILES = (d,)
            r = guide.choice_rows()[0]
            self.assertTrue(r["error"], "全调用失败必须记成测不了")
            self.assertEqual(r["failed"], [], "测不了就不该列「卡在哪」")
        finally:
            guide.CHOICE_FILES = saved

    def test_missing_file_is_not_an_error(self):
        saved = guide.CHOICE_FILES
        try:
            guide.CHOICE_FILES = (Path("/nonexistent/nope.json"),)
            self.assertEqual(guide.choice_rows(), [])
            self.assertEqual(guide.choice_of("x"), {})
        finally:
            guide.CHOICE_FILES = saved

    def test_truth_lines_mentions_both_gates(self):
        """对外文案必须**同时**讲清两位门槛，并明说它们不能互相取代。"""
        txt = "\n".join(guide.truth_lines())
        self.assertTrue("编排" in txt or "选动作" in txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
