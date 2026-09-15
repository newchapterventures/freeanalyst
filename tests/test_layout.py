"""`ingest/layout.py` 的测试 —— 无框线排版表的按行解析。

**这里的每一条都是被真实年报逼出来的**，不是想出来的：
宝宝树 2020 年报（H 股）的财务报表没有框线，`pdfplumber` 的默认策略
只抽到数字列，标签全丢。
"""

import unittest

from ingest.layout import _starts_lower, parse_layout_lines


def labels(rows):
    return [r[0] for r in rows]


def values(rows):
    return [(r[2], r[3]) for r in rows]


class TestBasicRows(unittest.TestCase):
    def test_simple_row(self):
        rows = parse_layout_lines("Trade payables 貿易應付款項 23 20,282 13,660")
        self.assertEqual(rows[0][0], "Trade payables 貿易應付款項")
        self.assertEqual(rows[0][1], "23")          # 附注号单独一列
        self.assertEqual((rows[0][2], rows[0][3]), ("20,282", "13,660"))

    def test_note_is_not_in_label(self):
        """**附注号不能留在标签里** —— 带着「22」去匹配科目名匹配不上。"""
        rows = parse_layout_lines("Cash and bank balances 現金及銀行結餘 22 1,079,716 1,422,855")
        self.assertEqual(rows[0][0], "Cash and bank balances 現金及銀行結餘")
        self.assertEqual(rows[0][1], "22")

    def test_no_note_column(self):
        rows = parse_layout_lines("Total equity 權益總額 2,314,754 2,314,754")
        self.assertEqual(rows[0][0], "Total equity 權益總額")
        self.assertEqual((rows[0][2], rows[0][3]), ("2,314,754", "2,314,754"))

    def test_dash_is_kept(self):
        """破折号表示零/无 —— 要留着，不能当成没有值。"""
        rows = parse_layout_lines("Prepayments 非流動資產預付款項 14 – 44,809")
        self.assertEqual((rows[0][2], rows[0][3]), ("–", "44,809"))


class TestSplitNumbers(unittest.TestCase):
    """右对齐的大数字会被 PDF 从中间断开。"""

    def test_split_number_is_merged(self):
        rows = parse_layout_lines("Cash and bank balances 現金及銀行結餘 22 1,079,71 6 1,422,855")
        self.assertEqual((rows[0][2], rows[0][3]), ("1,079,716", "1,422,855"))

    def test_intact_numbers_are_not_merged(self):
        """`20,282 13,660` 最后一组都是 3 位 —— 不能被误合并。"""
        rows = parse_layout_lines("Trade payables 貿易應付款項 23 20,282 13,660")
        self.assertEqual((rows[0][2], rows[0][3]), ("20,282", "13,660"))

    def test_bare_totals_line(self):
        rows = parse_layout_lines("2,012,45 1 2,662,441")
        self.assertEqual((rows[0][2], rows[0][3]), ("2,012,451", "2,662,441"))


class TestParenSpace(unittest.TestCase):
    """`(601,579 )` —— 右括号前有空格，会被切成两个词元。"""

    def test_paren_negative(self):
        rows = parse_layout_lines("Total comprehensive expense 年內全面開支總額 (601,579 ) (453,972 )")
        self.assertEqual((rows[0][2], rows[0][3]), ("(601,579)", "(453,972)"))

    def test_small_paren_negative(self):
        rows = parse_layout_lines("Non-controlling interests 非控制性權益 (682 ) (55 5)")
        self.assertEqual(rows[0][2], "(682)")


class TestWrappedLabels(unittest.TestCase):
    """长标签折行 —— 值留在最后一行。"""

    def test_two_line_wrap(self):
        text = (
            "Total comprehensive expense 年內全面開支總額\n"
            "for the year (601,579 ) (453,972 )\n"
        )
        rows = parse_layout_lines(text)
        self.assertEqual(rows[0][0], "Total comprehensive expense 年內全面開支總額 for the year")
        self.assertEqual(rows[0][2], "(601,579)")

    def test_three_line_wrap_with_section_headers_between(self):
        """**这是真材料里的原文。**

        实际 PDF 里长这样：标签折了三行，中间还夹着两个段标题。
        拼接要从下往上接，接不上的（段标题）单独成行。
        """
        text = (
            "Other comprehensive (expense)/ 年內其他全面（開支）╱收入\n"
            "income for the year (after taxation （經扣除稅項及作出重新\n"
            "and reclassification adjustments) 分類調整）\n"
            "Items that may be reclassified 其後可重新分類至損益的\n"
            "subsequently to profit or loss: 項目：\n"
            "Exchange differences on translation of 換算本公司及海外附屬公司\n"
            "financial statements of the Company 財務報表的匯兌差額\n"
            "and overseas subsidiaries (130,527) 40,455\n"
        )
        rows = parse_layout_lines(text)
        got = labels(rows)
        self.assertIn(
            "Exchange differences on translation of 換算本公司及海外附屬公司 "
            "financial statements of the Company 財務報表的匯兌差額 "
            "and overseas subsidiaries",
            got,
        )
        # 段标题没有被接到值行上，而是单独成行
        self.assertIn("Items that may be reclassified 其後可重新分類至損益的 "
                      "subsequently to profit or loss: 項目：", got)

    def test_starts_lower(self):
        self.assertTrue(_starts_lower("financial statements"))
        self.assertFalse(_starts_lower("Exchange differences"))
        self.assertFalse(_starts_lower("本公司權益股東"))     # 中文没有大小写
        self.assertFalse(_starts_lower(""))


class TestUnlabeledRows(unittest.TestCase):
    """**标签根本不在文字层里的行，不能被静默丢掉。**

    实测：宝宝树年报「Total current assets」那行只有数字，
    标签被画成了图形。丢掉的话，用户会以为这张表本来就没这一行。
    """

    def test_unlabeled_row_is_marked(self):
        rows = parse_layout_lines("1,079,716 1,422,855")
        self.assertEqual(len(rows), 1)
        self.assertIn("没有标签", rows[0][0])
        self.assertEqual(rows[0][2], "1,079,716")

    def test_label_only_lines_become_rows(self):
        rows = parse_layout_lines("Non-current assets 非流動資產\n")
        self.assertEqual(labels(rows), ["Non-current assets 非流動資產"])
        self.assertEqual(rows[0][2], "")


if __name__ == "__main__":
    unittest.main()
