"""HTML 抽取测试。

材料是 **Fitbit FY2016 10-K 的真实申报文件**（SEC EDGAR 公开数据），
不是合成样例。它带来两个合成样例给不出的东西：

1. **真实的表格结构** —— colspan/rowspan、多级表头、分节行
2. **官方 XBRL 标签内嵌在 HTML 里**（`defref_us-gaap_XXX`）

于是可以做一件合成样例做不到的事：**拿解析结果跟官方机器可读数据对账。**

## 这个文件锁住的三个 bug，都是「不报错的错」

**① `$ (102,777)` 的负号被吃掉** —— 括号判断写在剥货币符号之前，
   于是 `$ (` 开头的单元格判不出括号，负数变正数。
   **后果是亏损显示成盈利。** 靠跟官方 XBRL 对账才发现。

**② `<link>` 没有结束标签，skip 计数只增不减** ——
   整个文档被当成"在 skip 标签里"，返回 0 张表，不报错。

**③ XBRL 标签挂在 `<a>` 上，不在 `<td>` 上** ——
   在 `<td>` 属性里找，一行都找不到。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import html as ih  # noqa: E402

FITBIT = Path(__file__).resolve().parent.parent / "materials" / "fitbit-2016-10k"
BALANCE = FITBIT / "R2.htm"
INCOME = FITBIT / "R4.htm"


class TestToNumberSigns(unittest.TestCase):
    """**这是整个项目最危险的一处。**

    它把亏损变成盈利，而且不报错。
    """

    def test_dollar_paren_negative(self):
        """实测发现的 bug：`$ (102,777)` 原来被解析成 **正** 102777。"""
        self.assertEqual(ih._to_number("$ (102,777)"), -102777)
        self.assertEqual(ih._to_number("$ (0.47)"), -0.47)
        self.assertEqual(ih._to_number("$ (1,234.56)"), -1234.56)

    def test_bare_paren_negative(self):
        self.assertEqual(ih._to_number("(102,777)"), -102777)
        self.assertEqual(ih._to_number("(7,704)"), -7704)

    def test_positive_forms(self):
        self.assertEqual(ih._to_number("$ 301,320"), 301320)
        self.assertEqual(ih._to_number("301,320"), 301320)
        self.assertEqual(ih._to_number("$ 2,169,461"), 2169461)

    def test_explicit_minus(self):
        self.assertEqual(ih._to_number("-978"), -978)
        self.assertEqual(ih._to_number("$ -5"), -5)

    def test_empty_and_dash(self):
        """破折号在财报里表示零或"无"，不是数字。"""
        for s in ["", "—", "–", "-", "N/A"]:
            self.assertIsNone(ih._to_number(s), f"{s!r} 应该返回 None")

    def test_sign_survives_the_whole_pipeline(self):
        """修完之后，"$ (x)" 和 "(x)" 必须给出同一个数。"""
        a = ih._to_number("$ (102,777)")
        b = ih._to_number("(102,777)")
        self.assertIsNotNone(a)
        self.assertEqual(a, b)


class TestCellFragments(unittest.TestCase):
    """SEC 会把一个数字拆进多个 `<td>` —— 不拼回去就会丢符号。"""

    def test_close_paren_fragment_merges_backward(self):
        """实测：`<td colspan="2">(3,552</td><td>)</td>`。

        不合并的话 `_to_number("(3,552")` 判不出括号，**正负颠倒**。
        """
        out = ih._normalize_cells(["(3,552", "", ")"])
        self.assertEqual(out[0], "(3,552)")
        self.assertEqual(ih._to_number(out[0]), -3552)

    def test_currency_fragment_merges_forward(self):
        out = ih._normalize_cells(["$", "", "301,320"])
        self.assertEqual(ih._to_number(out[0]), 301320)

    def test_multiple_fragments_in_one_row(self):
        out = ih._normalize_cells(["$", "2,169,461", "", "$", "1,857,998"])
        nums = [ih._to_number(c) for c in out if c.strip()]
        self.assertEqual(nums, [2169461, 1857998])

    def test_open_paren_alone(self):
        out = ih._normalize_cells(["(", "7,704", ")"])
        self.assertEqual(ih._to_number(out[0]), -7704)

    def test_normal_cells_untouched(self):
        cells = ["1,234", "5,678", "9,012"]
        self.assertEqual(ih._normalize_cells(cells), cells)

    def test_column_count_is_preserved(self):
        """合并不能改变列数 —— 否则行列对应就乱了。"""
        for cells in (["(3,552", "", ")"], ["$", "", "301,320"], ["1", "", "2"]):
            self.assertEqual(len(ih._normalize_cells(cells)), len(cells))


@unittest.skipUnless(BALANCE.exists(), "需要 materials/fitbit-2016-10k（SEC 公开数据）")
class TestRealSecFiling(unittest.TestCase):
    """拿真实 10-K 申报文件测。"""

    @classmethod
    def setUpClass(cls):
        cls.balance = ih.extract_html(BALANCE)
        cls.income = ih.extract_html(INCOME)

    def test_tables_are_found(self):
        """**第二个 bug 的回归测试。**

        `<link>` 是自闭合标签，没有 `</link>`。如果 skip 计数只增不减，
        整个文档会被跳过，这里会返回 0 张表。
        """
        self.assertGreater(len(self.balance.tables), 0,
                           "一张表都没抽到 —— 检查自闭合标签的 skip 计数")

    def test_balance_sheet_title_and_columns(self):
        t = self.balance.tables[0]
        self.assertIn("Balance Sheet", t.header[0])
        self.assertIn("2016", t.header[1])
        self.assertIn("2015", t.header[2])

    def test_known_values(self):
        """数字对不对 —— 手抄自年报原文。"""
        t = self.balance.tables[0]
        rows = {r.label: r.numeric_cells() for r in t.rows}
        self.assertEqual(rows["Cash and cash equivalents"][0], 301320)
        self.assertEqual(rows["Total assets"][0], 1820226)
        self.assertEqual(rows["Total current liabilities"][0], 761932)

    def test_income_statement_negative_net_income(self):
        """FY2016 是亏损年 —— **负号必须活下来**。"""
        t = self.income.tables[0]
        rows = {r.label: r.numeric_cells() for r in t.rows}
        self.assertEqual(rows["Net income (loss)"][0], -102777)
        self.assertEqual(rows["Operating income (loss)"][0], -112465)

    def test_nearly_every_row_has_an_xbrl_tag(self):
        """**第三个 bug 的回归测试。**

        标签挂在单元格内部的 `<a>` 上，不在 `<td>` 上。
        """
        t = self.balance.tables[0]
        tagged = [r for r in t.rows if r.xbrl_tag]
        self.assertGreater(len(tagged), len(t.rows) * 0.8,
                           "大部分行应该有 XBRL 标签 —— 检查是不是只在 <td> 属性里找")

    def test_tags_are_official_us_gaap(self):
        t = self.balance.tables[0]
        rows = {r.label: r.xbrl_tag for r in t.rows}
        self.assertEqual(rows["Total assets"], "us-gaap:Assets")
        self.assertEqual(rows["Cash and cash equivalents"],
                         "us-gaap:CashAndCashEquivalentsAtCarryingValue")
        self.assertEqual(rows["Total current assets"], "us-gaap:AssetsCurrent")

    def test_duplicate_tags_exist(self):
        """**`标签 → 数值` 不是一对一映射**，科目映射时必须知道这点。

        Fitbit 有 A/B 两类普通股，HTML 分行列示（18 / 5），
        而 XBRL 用一个标签给合计（23,000）。
        """
        t = self.balance.tables[0]
        tags = [r.xbrl_tag for r in t.rows if r.xbrl_tag]
        self.assertIn("us-gaap:CommonStockValue", tags)
        commons = [r for r in t.rows if r.xbrl_tag == "us-gaap:CommonStockValue"]
        self.assertEqual(len(commons), 2, "两类普通股应该各占一行")
        # 两行的值不一样 —— 所以不能按标签取值反推某一行
        self.assertNotEqual(commons[0].numeric_cells()[0],
                            commons[1].numeric_cells()[0])


if __name__ == "__main__":
    unittest.main()
