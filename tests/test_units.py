"""单位纪律检查的测试 —— 两次真实失败逼出来的。

实测踩到两次，都是**数量级错误**：

    宁波精塑 CIM（万元）
      材料  2025 年营业收入 18,300 万元        = 1.83 亿
      模型  Revenue 18,300 million RMB          ← 差 100 倍

    Fitbit 10-K（千美元）
      材料  Operating income (loss) (112,465)  = 1.12465 亿
      模型  营业利润 -112,465千美元（即亏损 11.25 亿美元）  ← 差 10 倍

两次的原文数字都在旁边、看着都对，只有换算是错的。

**不禁止换算**（跨语言回答时把「万元」讲清楚是必要的），
但换算必须落在材料里真实存在的某个金额上。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))

import model_quality as mq  # noqa: E402
from retrieval import Chunk  # noqa: E402


def _chunk(text: str) -> list[tuple[Chunk, float]]:
    return [(Chunk("S1", "m.txt", 1, 2, text), 1.0)]


CN = "2025 年营业收入 18,300 万元，报表 EBITDA 3,150 万元。"
EN = ("【表 1】Consolidated Balance Sheets - USD ($) $ in Thousands\n"
      "| Operating income (loss) | (112,465) | 348,198 |")


class TestDeclaredUnit(unittest.TestCase):
    """**不做这一步，材料侧的金额一个都抽不到。**

    财务报表的数字都在表里，单位写在表头，行里只有裸数字。
    """

    def test_chinese_declaration(self):
        self.assertEqual(mq.declared_unit("（单位：万元）"), 1e4)
        self.assertEqual(mq.declared_unit("单位：千美元"), 1e3)

    def test_english_declaration(self):
        self.assertEqual(mq.declared_unit("$ in Thousands"), 1e3)
        self.assertEqual(mq.declared_unit("(In thousands)"), 1e3)
        self.assertEqual(mq.declared_unit("USD in millions"), 1e6)

    def test_no_declaration(self):
        self.assertIsNone(mq.declared_unit("普通段落，没有单位声明"))


class TestAmountExtraction(unittest.TestCase):
    def test_chinese_units(self):
        vals = [v for v, _ in mq._amounts("18,300 万元")]
        self.assertEqual(vals, [1.83e8])

    def test_english_units(self):
        """跨语言回答时模型用英文单位词 —— 实测里这是漏掉的一类。"""
        self.assertEqual([v for v, _ in mq._amounts("18,300 million RMB")], [1.83e10])
        self.assertEqual([v for v, _ in mq._amounts("1.5 billion USD")], [1.5e9])

    def test_bare_numbers_need_declaration(self):
        """裸数字不按金额算（答案里的裸数字多是年份、页码）。"""
        self.assertEqual(mq._amounts("112,465"), [])
        self.assertEqual(
            [v for v, _ in mq._amounts("$ in Thousands\n112,465",
                                       apply_declared=True)],
            [1.12465e8],
        )


class TestCheckUnits(unittest.TestCase):
    def test_wrong_conversion_is_caught(self):
        """实测失败 1：18,300 万元 写成 18,300 million（差 100 倍）。"""
        r = mq.check_units("营业收入 18,300 million RMB", _chunk(CN))
        self.assertTrue(r, "应该抓到数量级错误")

    def test_correct_conversion_passes(self):
        """1.83 亿 = 18,300 万元 ✓"""
        self.assertEqual(mq.check_units("营业收入 1.83 亿元", _chunk(CN)), [])

    def test_wrong_conversion_caught_english(self):
        """实测失败 2：千美元 当成 万美元（差 10 倍）。"""
        r = mq.check_units("营业利润 -112,465千美元（即亏损 11.25 亿美元）",
                           _chunk(EN))
        self.assertTrue(r, "应该抓到数量级错误")

    def test_correct_conversion_passes_english(self):
        r = mq.check_units("营业利润 -112,465千美元（即亏损 1.12465 亿美元）",
                           _chunk(EN))
        self.assertEqual(r, [])

    def test_as_is_quoting_passes(self):
        self.assertEqual(mq.check_units("营业收入 18,300 万元", _chunk(CN)), [])

    def test_no_amounts_is_not_a_failure(self):
        """答案里没有金额时不该报错 —— 那一节可能确实没有金额。"""
        self.assertEqual(mq.check_units("材料未提供。", _chunk(CN)), [])


if __name__ == "__main__":
    unittest.main()
