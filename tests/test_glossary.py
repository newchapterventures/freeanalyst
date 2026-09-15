"""跨语言术语表测试。

## 要锁住的几件事

**① 长词优先** —— 「营业利润」里含「利润」。如果两个都扩展，
   查询会同时带上 `operating income` 和 `income`，
   后者是噪音词。实测中它会让检索跑偏。

**② 双向** —— 中文问英文材料、英文问中文材料，用的是同一张表。

**③ 不命中就不动** —— 查询里没有任何术语时，原样返回，
   不能凭空塞词进去（那会污染检索）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glossary import (  # noqa: E402
    GLOSSARY,
    detect_language,
    expand_query,
    expand_terms,
)


def added(query: str) -> str:
    return expand_query(query)[len(query):].strip()


class TestLongestFirst(unittest.TestCase):
    def test_operating_income_beats_income(self):
        """「营业利润」占住位置后，「利润」不能再单独匹配。

        否则会多出一个**独立的** `income` —— 会命中大量无关片段。

        按词的**列表**判断，不是按字符串 —— 否则
        `operating income` 分词后也含 `income`，两者分不开。
        """
        terms = expand_terms("营业利润是多少？")
        self.assertIn("operating income", terms)
        self.assertNotIn("income", terms, "出现了独立的 income —— 长词优先没生效")

    def test_bare_profit_alone_still_expands(self):
        """但单独问「利润」时，该给 `income`/`profit`。"""
        self.assertTrue(expand_terms("利润是多少？"))

    def test_net_margin_beats_margin(self):
        a = added("净利润率是多少？")
        self.assertIn("net margin", a)

    def test_total_assets_beats_assets(self):
        a = added("总资产是多少？")
        self.assertIn("total assets", a)

    def test_current_assets_distinct_from_total(self):
        a = added("流动资产和总资产各是多少？")
        self.assertIn("current assets", a)
        self.assertIn("total assets", a)


class TestBidirectional(unittest.TestCase):
    def test_chinese_query_gets_english(self):
        a = added("营业收入和净利润")
        self.assertIn("revenue", a)
        self.assertIn("net income", a)

    def test_english_query_gets_chinese(self):
        a = added("revenue and net income")
        self.assertIn("营业收入", a)
        self.assertIn("净利润", a)

    def test_xbrl_tag_reachable_from_chinese(self):
        """「总资产」应该能扩展出 `Assets` —— 官方 XBRL 标签，
        它出现在索引里的表格标题中（见 ingest/html.py）。"""
        a = added("总资产")
        self.assertIn("Assets", a)


class TestNoOpCases(unittest.TestCase):
    def test_query_without_terms_is_untouched(self):
        q = "今天天气怎么样"
        self.assertEqual(expand_query(q), q)

    def test_empty_query(self):
        self.assertEqual(expand_query(""), "")

    def test_whitespace_query(self):
        self.assertEqual(expand_query("   "), "   ")


class TestGlossaryHygiene(unittest.TestCase):
    def test_no_duplicate_chinese_terms(self):
        zh = [t.zh for t in GLOSSARY]
        dupes = {x for x in zh if zh.count(x) > 1}
        self.assertFalse(dupes, f"中文术语重复：{dupes}")

    def test_every_term_has_both_languages(self):
        for t in GLOSSARY:
            self.assertTrue(t.zh.strip(), f"{t} 缺中文")
            self.assertTrue(t.en.strip(), f"{t} 缺英文")

    def test_basics_are_present(self):
        """三张表最核心的科目一个都不能少。"""
        zh = {t.zh for t in GLOSSARY}
        for required in ["营业收入", "营业成本", "净利润", "总资产", "总负债",
                         "货币资金", "应收账款", "存货", "经营活动现金流"]:
            self.assertIn(required, zh, f"术语表缺「{required}」")


class TestDetectLanguage(unittest.TestCase):
    def test_chinese(self):
        self.assertEqual(detect_language("营业收入是多少"), "zh")

    def test_english(self):
        self.assertEqual(detect_language("What is the revenue"), "en")

    def test_mixed(self):
        self.assertEqual(detect_language("Fitbit 的 revenue 是多少"), "zh")

    def test_numbers_only(self):
        self.assertEqual(detect_language("2016"), "en")


class TestIdempotence(unittest.TestCase):
    def test_expanding_twice_does_not_pile_up(self):
        """扩展结果里已经有的词，不该再加一遍。"""
        once = expand_query("营业收入")
        twice = expand_query(once)
        self.assertEqual(once.count("revenue"), twice.count("revenue"))


if __name__ == "__main__":
    unittest.main()
