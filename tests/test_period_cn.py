"""报表期间：中文数字年份也要认（实测一份审计报告只写了「二〇二四年度」）。

## 为什么这条要修
那份审计报告的期间**只有中文数字写法** → 期间判不出来 →
估值基准日退化成「1…N」占位 → 报告里多一条本可避免的缺口
（"预测年份标签（估值基准日里没有年份）"）。

## 两条纪律
1. **只认年报那套小写中文数字**（〇零一二三…），不认「壹贰叁」——
   那是**金额**的大写写法，拿来当年份会认错东西。
2. **材料只写年度就只说年度**：不替它补「12月31日」——
   编一个日期比不给更坏（价格、多期对齐都会跟着错）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials.meta import detect_period                   # noqa: E402


class TestPeriodChineseYear(unittest.TestCase):
    def test_chinese_year_is_recognised(self):
        p = detect_period("二〇二四年度财务报表")
        self.assertIn("2024", p)
        self.assertIn("二〇二四", p)        # 说清原表怎么写

    def test_zero_variants(self):
        self.assertIn("2024", detect_period("二零二四年度"))
        self.assertIn("2019", detect_period("二〇一九年度"))

    def test_no_month_is_invented(self):
        """材料只写年度 → **不许补月份和日**。"""
        p = detect_period("二〇二四年度")
        self.assertNotIn("12月", p)
        self.assertNotIn("31日", p)

    def test_arabic_wins_when_present(self):
        """阿拉伯数字写法优先（更精确，而且原来就认）。"""
        self.assertEqual(detect_period("2024年12月31日"), "2024年12月31日")

    def test_uppercase_financial_numerals_are_not_years(self):
        """「壹贰叁」是**金额**的大写，不该被当年份。"""
        self.assertEqual(detect_period("金额壹贰叁肆元整"), "")

    def test_impossible_year_is_rejected(self):
        self.assertEqual(detect_period("八八九九年度"), "")

    def test_nothing_returns_empty(self):
        self.assertEqual(detect_period(""), "")
        self.assertEqual(detect_period("本公司主营业务为电子元件制造"), "")


if __name__ == "__main__":
    unittest.main()
