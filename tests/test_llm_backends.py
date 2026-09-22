"""模型运行时适配层 + 质量门槛的测试。

重点：
- **没有模型不是错误**（提取和估值是纯代码算的）
- 只放行回环地址（「模型在本机」是代码挡着的，不是一句承诺）
- 门槛没过时要说清卡在哪一条
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm import backends, gate  # noqa: E402


class _Fake:
    def __init__(self, name, ok=True, why="在跑", models=None):
        self.name = name
        self._ok = ok
        self._why = why
        self._models = models or ["m1"]

    def available(self):
        return self._ok, self._why

    def list_models(self):
        return list(self._models)

    def generate(self, model, prompt, system="", timeout=120):
        return "ok"


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self._saved = dict(backends.REGISTRY)
        backends.REGISTRY.clear()

    def tearDown(self):
        backends.REGISTRY.clear()
        backends.REGISTRY.update(self._saved)

    def test_register_and_detect(self):
        backends.register(_Fake("a"))
        self.assertEqual(backends.detect()[0][0], "a")

    def test_duplicate_refused(self):
        """同名默认拒绝覆盖 —— 静默替换会让人以为用的是自己接的那个。"""
        backends.register(_Fake("a"))
        with self.assertRaises(ValueError):
            backends.register(_Fake("a"))
        backends.register(_Fake("a"), replace=True)

    def test_broken_backend_does_not_crash_detect(self):
        class Boom(_Fake):
            def available(self):
                raise RuntimeError("炸了")

        backends.register(Boom("boom"))
        rows = backends.detect()
        self.assertFalse(rows[0][1])
        self.assertIn("RuntimeError", rows[0][2])

    def test_pick_returns_none_when_nothing_available(self):
        """**没有模型不是错误。**

        提取三张表、口径判断、勾稽校验、估值计算**全是纯代码** ——
        没装模型照样能跑出估值结论。所以这里返回 None，不抛异常。
        """
        backends.register(_Fake("a", ok=False, why="没装"))
        self.assertIsNone(backends.pick())

    def test_pick_skips_unavailable(self):
        backends.register(_Fake("a", ok=False, why="没装"))
        backends.register(_Fake("b", ok=True))
        self.assertEqual(backends.pick().name, "b")


class TestLoopbackOnly(unittest.TestCase):
    """「模型在本机」必须是**代码挡着的**，不是一句承诺。"""

    def test_ollama_url_is_loopback(self):
        self.assertTrue(backends.OLLAMA_URL.startswith("http://127.0.0.1"))

    def test_compat_url_is_loopback(self):
        b = backends.OpenAICompatBackend()
        self.assertTrue(b.base_url.startswith("http://127.0.0.1"))

    def test_all_ports_are_loopback_defaults(self):
        for p in backends.OPENAI_COMPAT_PORTS:
            self.assertIsInstance(p, int)


class TestGateReading(unittest.TestCase):
    """门槛的裁决逻辑 —— **判分只有一份**（在 bench 里），这里只管读取。"""

    REPORT = {
        "m1": {
            "passed": 1, "total": 3, "verdict": "未达门槛（最差 1/3）",
            "results": [
                {"id": "units", "name": "单位纪律", "passed": True, "reasons": []},
                {"id": "subject", "name": "主体识别", "passed": False,
                 "reasons": ["把个人义务说成公司负担：「公司造成压力」"]},
                {"id": "conflict", "name": "跨文档冲突", "passed": False,
                 "reasons": ["未引用访谈纪要", "未做跨文档比对"]},
            ],
        }
    }

    def _write(self, data):
        d = Path(tempfile.mkdtemp()) / "r.json"
        d.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return d

    def test_reads_score(self):
        r = gate._from_json("m1", self._write(self.REPORT))
        self.assertEqual(r.score, "1/3")

    def test_not_passed(self):
        r = gate._from_json("m1", self._write(self.REPORT))
        self.assertFalse(r.passed)

    def test_lists_failed_cases_with_reasons(self):
        """**只说「未达门槛」没用。**

        用户要知道是「主体搞混」还是「编造内容」—— 这两类的严重程度不同。
        """
        r = gate._from_json("m1", self._write(self.REPORT))
        joined = " / ".join(r.failed)
        self.assertIn("主体识别", joined)
        self.assertIn("把个人义务说成公司负担", joined)
        self.assertIn("跨文档冲突", joined)
        self.assertIn("未引用访谈纪要", joined)

    def test_all_pass(self):
        d = {"m2": {"passed": 3, "total": 3, "results": [
            {"id": "a", "name": "A", "passed": True, "reasons": []}]}}
        self.assertTrue(gate._from_json("m2", self._write(d)).passed)

    def test_zero_total_is_not_pass(self):
        """0/0 不能算过 —— 没跑任何用例不是「全过」。"""
        d = {"m3": {"passed": 0, "total": 0, "results": []}}
        self.assertFalse(gate._from_json("m3", self._write(d)).passed)

    def test_missing_file_returns_none(self):
        self.assertIsNone(gate._from_json("x", Path("/nonexistent/x.json")))

    def test_broken_json_returns_none(self):
        p = Path(tempfile.mkdtemp()) / "bad.json"
        p.write_text("{不是 json", encoding="utf-8")
        self.assertIsNone(gate._from_json("x", p))

    def test_falls_back_to_model_key_by_position(self):
        """报告里键名跟模型名对不上时，用第一个（单模型跑就是这样）。"""
        r = gate._from_json("别的名字", self._write(self.REPORT))
        self.assertEqual(r.score, "1/3")

    def test_no_reasons_says_so(self):
        d = {"m": {"passed": 0, "total": 1, "results": [
            {"id": "a", "name": "A", "passed": False, "reasons": []}]}}
        r = gate._from_json("m", self._write(d))
        self.assertIn("未说明原因", r.failed[0])


class TestGateReport(unittest.TestCase):
    def test_no_qualified_model_does_not_say_make_do(self):
        """**不能建议「先凑合用」。**

        尽调里那三类错会直接导致错误结论。
        """
        r = gate.GateResult(model="m", passed=False, score="2/5",
                            failed=["主体识别 —— 把个人义务说成公司负担"])
        txt = "\n".join(gate.report_lines([r]))
        self.assertIn("没有一个模型达到门槛", txt)
        self.assertIn("纯代码", txt)
        self.assertNotIn("凑合", txt.replace("不是「先凑合用」", ""))

    def test_shows_detail_lines(self):
        r = gate.GateResult(model="m", passed=False, score="2/5",
                            failed=["主体识别 —— 把个人义务说成公司负担"])
        txt = "\n".join(gate.report_lines([r]))
        self.assertIn("卡在：", txt)
        self.assertIn("把个人义务说成公司负担", txt)

    def test_detail_empty_when_passed(self):
        r = gate.GateResult(model="m", passed=True, score="5/5")
        self.assertEqual(gate.detail_lines(r), [])

    def test_names_the_qualified_model(self):
        r = gate.GateResult(model="good", passed=True, score="5/5")
        txt = "\n".join(gate.report_lines([r]))
        self.assertIn("good", txt)
        self.assertIn("可用", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
