"""附注取数（折旧摊销）的测试。

用假文档，不依赖真实 PDF —— 但**原文片段照抄实测材料**，
包括那些让第一版翻车的写法。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from financials import notes  # noqa: E402


class _Page:
    def __init__(self, number, text):
        self.number, self.text = number, text


class _Doc:
    def __init__(self, pages):
        self.pages = pages


#: 照抄茅台第 115 页的实际排版 —— **数字插在标签中间**
MT = _Page(115, (
    "补充资料本期金额上期金额 1.将净利润调节为经营活动现金流量: "
    "净利润 85,310,324,833.67 89,334,728,025.90 "
    "加:资产减值准备信用减值损失 -17,234,379.37 23,248,436.03 "
    "固定资产折旧、油气资产折耗、生产 1,893,338,311.91 1,721,165,327.14 "
    "性生物资产折旧使用权资产摊销 55,797,324.89 94,492,678.29 "
    "无形资产摊销 289,613,682.99 249,170,059.35 "
    "长期待摊费用摊销 20,637,734.49 20,191,550.34"
))


class TestFindReconciliation(unittest.TestCase):
    def test_finds_section(self):
        self.assertEqual(notes.find_reconciliation_pages(_Doc([MT])), [115])

    def test_none_when_absent(self):
        self.assertEqual(
            notes.find_reconciliation_pages(_Doc([_Page(1, "资产负债表 货币资金")])),
            [])


class TestExtractDa(unittest.TestCase):
    def test_maotai_total_matches_hand_calculation(self):
        """茅台 2025：22.6 亿。

        手算：1,893,338,311.91 + 55,797,324.89 + 289,613,682.99
              + 20,637,734.49 = 2,259,387,054.28
        """
        da = notes.extract_da(_Doc([MT]))
        self.assertAlmostEqual(da.total, 2_259_387_054.28, places=2)
        self.assertEqual(da.pages, [115])

    def test_combined_line_not_double_counted(self):
        """**回归：一行挂三个标签，只能算一次。**

        源文里是 `固定资产折旧、油气资产折耗、生产性生物资产折旧 1,893,338,311.91`
        —— 三个前缀都会去取它后面第一个金额，取到同一个数。

        第一版没有去重，茅台的 D&A 从 22.6 亿算成 **41.5 亿**（虚增近一倍），
        而且不报错。
        """
        da = notes.extract_da(_Doc([MT]))
        vals = list(da.components.values())
        self.assertEqual(len(vals), len(set(vals)), f"有重复计数：{da.components}")
        # 1,893,338,311.91 只能出现一次
        self.assertEqual(vals.count(1_893_338_311.91), 1)

    def test_components_named(self):
        da = notes.extract_da(_Doc([MT]))
        self.assertIn("固定资产折旧", da.components)
        self.assertIn("无形资产摊销", da.components)
        self.assertIn("使用权资产摊销", da.components)
        self.assertIn("长期待摊费用摊销", da.components)

    def test_no_section_says_so(self):
        """取不到时必须**明说**，不能悄悄给 0。"""
        da = notes.extract_da(_Doc([_Page(1, "资产负债表")]))
        self.assertIsNone(da.total)
        self.assertIn("找不到", da.note)

    def test_zero_values_skipped(self):
        """全零的行不该进合计 —— 会让合计看起来像「有数」。"""
        doc = _Doc([_Page(5, "将净利润调节为经营活动现金流量 无形资产摊销 0")])
        da = notes.extract_da(doc)
        self.assertIsNone(da.total)

    def test_management_expense_breakdown_not_taken_as_da(self):
        """**只用调节段里那一行。**

        年报里折旧摊销出现很多次。实测茅台第 109 页的「管理费用明细」里也有
        `固定资产折旧费用 659,934,804.32` —— 那只是**计入管理费用的那部分**，
        不是全部的 D&A（真实的固定资产折旧是 1,893,338,311.91）。
        拿它当 D&A 会严重低估，**而且不会报错**。
        """
        mgmt = _Page(109, "单位:元项目本期发生额上期发生额职工薪酬费用 2,804,105,074.93 "
                          "固定资产折旧费用 659,934,804.32 612,955,726.82 "
                          "无形资产摊销 289,521,523.31 249,168,259.35")
        da = notes.extract_da(_Doc([mgmt]))
        self.assertIsNone(da.total, "管理费用明细不该被当成调节段")


class TestNumberParsing(unittest.TestCase):
    def test_thousands_separator(self):
        self.assertEqual(notes._to_number("1,893,338,311.91"), 1_893_338_311.91)

    def test_parenthesised_negative(self):
        self.assertEqual(notes._to_number("(17,234,379.37)"), -17_234_379.37)

    def test_plain_negative(self):
        self.assertEqual(notes._to_number("-17,234,379.37"), -17_234_379.37)


if __name__ == "__main__":
    unittest.main()
