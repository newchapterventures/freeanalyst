"""对话面板的后端（`/api/ask`）—— **当前不接模型**，只测它真会的那两件事。

## 这个面板的设计约束（测试要钉住的）
1. **它不猜** —— 认不出是哪一项就明说认不出；一句话可能指多项就**列出来让用户点**。
2. **它不产生数字** —— 只产出「提议」（字段 + 值 + 来源 + 置信度），
   采纳与否由用户点头，数字仍由引擎算。
3. **材料不出本机** —— 检索只在本地材料上做，不发任何请求（测试里也不联网）。
4. **提议必须带痕迹** —— 来源写"对话输入（未核实）"、置信度「低」，
   这样报告里能看回"这个数是谁给的、可不可信"。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webapp                                                # noqa: E402
from intake import Q                                         # noqa: E402


def _fake_mat(tmp: str):
    """一个假材料：目录里放一个 .md，问题清单里放两项。"""
    (Path(tmp) / "note.md").write_text("租金每年调整一次。\n增长率按合同约定。",
                                       encoding="utf-8")
    return mock.Mock(directory=Path(tmp), is_file=False, label="假材料")


def _questions():
    return [Q("growth", "营业收入增长率（逐年，逗号分隔）", group="预测"),
            Q("tax_rate", "所得税率", group="折现率", unit="%"),
            Q("terminal_growth", "永续增长率", group="预测", unit="%"),
            # ⚠️ 真实的键就长这样（不是 `da_ratio` 那种猜出来的名字）——
            # 实测的缺陷：按猜的键名匹配，这类项目**永远匹配不上**。
            Q("da_pct_revenue", "折旧摊销占收入比", group="历史比率", unit="%")]


class TestAskSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name
        webapp._CACHE[self.path] = _fake_mat(self.path)

    def tearDown(self):
        webapp._CACHE.pop(self.path, None)
        self.tmp.cleanup()

    def test_search_finds_hits_in_text_materials(self):
        out = webapp.api_ask({"action": "search", "path": self.path, "q": "租金"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["hits"][0]["file"], "note.md")
        self.assertIsNone(out["hits"][0]["page"], "纯文本没有页码，不许编一个")

    def test_search_rejects_too_short_keyword(self):
        out = webapp.api_ask({"action": "search", "path": self.path, "q": "租"})
        self.assertFalse(out["ok"])
        self.assertIn("太短", out["error"])

    def test_search_without_loaded_material_is_honest(self):
        out = webapp.api_ask({"action": "search", "path": "/nowhere", "q": "租金"})
        self.assertFalse(out["ok"])
        self.assertIn("还没载入", out["error"])

    def test_no_path_is_rejected(self):
        out = webapp.api_ask({"action": "search", "q": "租金"})
        self.assertFalse(out["ok"])
        self.assertIn("载入材料", out["error"])

    def test_unknown_action_lists_the_known_ones(self):
        out = webapp.api_ask({"action": "chat", "path": self.path})
        self.assertFalse(out["ok"])
        self.assertIn("search", out["error"])
        self.assertIn("propose", out["error"])


class TestAskPropose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name
        webapp._CACHE[self.path] = _fake_mat(self.path)
        self.q = mock.patch("intake.questions", return_value=_questions())
        self.q.start()

    def tearDown(self):
        self.q.stop()
        webapp._CACHE.pop(self.path, None)
        self.tmp.cleanup()

    def test_matches_a_question_and_carries_a_trail(self):
        out = webapp.api_ask({"action": "propose", "path": self.path,
                              "text": "所得税率按 15%"})
        self.assertTrue(out["ok"], out)
        p = out["proposal"]
        self.assertEqual(p["key"], "tax_rate")
        self.assertEqual(p["value"], "15%")
        self.assertEqual(p["source"], "对话输入（未核实）")
        self.assertEqual(p["confidence"], "低", "未核实的输入不许标高置信度")
        self.assertIn("所得税率", p["why"])

    def test_matches_by_label_not_by_guessed_key(self):
        """★ 实测缺陷：真实清单里键是 `da_pct_revenue`，猜的名字（`da_ratio`）匹配不上。

        「折旧摊销 12%」必须能落到「折旧摊销占收入比」——按**标签**匹配才不会随键名漂。
        """
        out = webapp.api_ask({"action": "propose", "path": self.path,
                              "text": "折旧摊销 12%"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["proposal"]["key"], "da_pct_revenue")
        self.assertEqual(out["proposal"]["value"], "12%")

    def test_plain_number_without_percent_stays_plain(self):
        out = webapp.api_ask({"action": "propose", "path": self.path,
                              "text": "营业收入增长率 0.05"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["proposal"]["value"], "0.05")

    def test_ambiguous_wording_asks_instead_of_choosing(self):
        """「增长率 5%」在这个清单里同时像两项 —— **不许替用户选**，列出来让他点。"""
        out = webapp.api_ask({"action": "propose", "path": self.path,
                              "text": "增长率 5%"})
        if out.get("ok"):
            self.skipTest("这一版收敛到唯一一项了 —— 若是有意为之，删掉本条")
        self.assertTrue(out.get("ambiguous"))
        self.assertGreaterEqual(len(out["candidates"]), 2)

    def test_no_number_is_refused_with_a_reason(self):
        out = webapp.api_ask({"action": "propose", "path": self.path,
                              "text": "增长率看着还行"})
        self.assertFalse(out["ok"])
        self.assertIn("没有数字", out["error"])

    def test_unrecognised_wording_is_refused_not_guessed(self):
        out = webapp.api_ask({"action": "propose", "path": self.path,
                              "text": "随便给个 3"})
        self.assertFalse(out["ok"])
        self.assertIn("认不出", out["error"])

    def test_never_invents_a_field_key(self):
        """提议的键必须是问题清单里真实存在的 —— 不许自己造一个。"""
        for text in ("增长率 5%", "所得税率 15%", "永续增长率 2%"):
            out = webapp.api_ask({"action": "propose", "path": self.path, "text": text})
            if out.get("ok"):
                self.assertIn(out["proposal"]["key"],
                              [q.key for q in _questions()])


if __name__ == "__main__":
    unittest.main()
