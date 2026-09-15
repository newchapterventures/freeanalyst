"""`ingest/ocr.py` 的测试。

**每一条都是被真实材料逼出来的**，取材自苏州井利电子 2024 年审计报告
（34 页纯扫描件，走 macOS Vision 框架）。

这里只测纯函数 —— 不碰 Swift、不碰文件，所以在任何机器上都能跑。
`available()` 单独测，它会随平台返回不同结果。
"""

import unittest

from ingest.ocr import (
    _is_row_number, _is_value_like, _runs, available, cluster_columns,
    is_amount_ish, parse_amount, repair_systematic, rows_to_table,
)


class TestParseAmount(unittest.TestCase):
    def test_valid(self):
        for text in ("6,840,705.08", "186", "-14,439,051.25", "0"):
            v, s = parse_amount(text)
            self.assertEqual(s, "ok", text)
            self.assertIsNotNone(v)

    def test_paren_negative(self):
        """中文和美股报表都用括号表示负数。"""
        self.assertEqual(parse_amount("(601,579)"), (-601579.0, "ok"))

    def test_currency_prefix(self):
        self.assertEqual(parse_amount("¥1,234.00"), (1234.0, "ok"))

    def test_dash_is_zero_not_missing(self):
        """破折号表示零 / 无 —— 和「没填」是两回事。"""
        for d in ("–", "—", "-", "N/A"):
            self.assertEqual(parse_amount(d), (None, "dash"), d)

    def test_empty(self):
        for e in ("", "   ", None):
            self.assertEqual(parse_amount(e), (None, "empty"), repr(e))

    def test_suspect_is_rejected_not_coerced(self):
        """**判据不明确的必须返回 None，绝不能凑一个数出来。**

        `334.719.50` 若按「剥掉非数字」处理会变成 33471950 ——
        差 100 倍且不报错。财务数据上这种静默错误最危险。
        """
        for bad in ("45.508.51239", "4 916.422 83", "505o18914969", "13291923"):
            v, s = parse_amount(bad)
            self.assertIsNone(v, bad)
            self.assertEqual(s, "suspect", bad)


class TestRepairSystematic(unittest.TestCase):
    """系统性 OCR 错误：千分位逗号被认成句点或空格。

    这不是随机噪声 —— 同一个字符被稳定认错，错误模型是确定的，
    所以可以按严格判据修。**随机错误不能用这招。**
    """

    def test_comma_read_as_period(self):
        self.assertEqual(repair_systematic("334.719.50"), "334,719.50")
        self.assertEqual(repair_systematic("1.609.705.35"), "1,609,705.35")
        self.assertEqual(repair_systematic("248.534.085.01"), "248,534,085.01")

    def test_comma_read_as_space(self):
        self.assertEqual(repair_systematic("85 748 813 69"), "85,748,813.69")

    def test_refuses_ambiguous(self):
        """**末组位数不对就不能猜。**

        `45.508.51239` 的末组是 5 位，归不了位 —— 它到底该是
        `45,508,512.39` 还是别的，无法从字面断定。宁可当可疑值剔掉。
        """
        for bad in ("45.508.51239", "4 916.422 83", "13291923", "65,890 422.07"):
            self.assertIsNone(repair_systematic(bad), bad)

    def test_leaves_valid_numbers_alone(self):
        self.assertIsNone(repair_systematic("6,840,705.08"))

    def test_parse_marks_repairs_distinctly(self):
        """修出来的数要能和原生的数分开统计。"""
        self.assertEqual(parse_amount("334.719.50"), (334719.50, "repaired"))
        self.assertEqual(parse_amount("6,840,705.08")[1], "ok")


class TestIsAmountIsh(unittest.TestCase):
    def test_plain_text_is_not_amount_ish(self):
        """**纯中文标签不能被当成「格式不对的金额」。**

        否则「资产负债表」「交易性金融资产」都会被算进值列，整张表错位。
        """
        for t in ("资产负债表", "货币资金", "流动资产合计", "单位：元"):
            self.assertFalse(is_amount_ish(t), t)

    def test_malformed_number_is_amount_ish(self):
        for t in ("334.719.50", "45.508.51239", "1,079,71"):
            self.assertTrue(is_amount_ish(t), t)

    def test_fullwidth_punctuation(self):
        """`202.，275,237.27` 混进全角逗号 —— 不认全角就报不出来。"""
        self.assertTrue(is_amount_ish("202.，275,237.27"))


class TestRowNumber(unittest.TestCase):
    def test_small_integer_is_row_number(self):
        for t in ("1", "14", "34", "75"):
            self.assertTrue(_is_row_number(t), t)

    def test_money_is_not_row_number(self):
        for t in ("6,840,705.08", "186.00", "601,579"):
            self.assertFalse(_is_row_number(t), t)


class TestRuns(unittest.TestCase):
    def test_contiguous(self):
        self.assertEqual(_runs([1, 2, 3, 7, 8, 10]),
                         [(1, 3), (7, 8), (10, 10)])

    def test_empty(self):
        self.assertEqual(_runs([]), [])


class TestColumnClustering(unittest.TestCase):
    def test_separate_columns(self):
        centers = cluster_columns([
            [(0.10, "a"), (0.50, "1"), (0.70, "2")],
            [(0.10, "b"), (0.50, "3"), (0.70, "4")],
        ])
        # 三个明显不同的 x → 三列
        self.assertEqual(len(centers), 3)

    def test_near_x_merges(self):
        centers = cluster_columns([[(0.10, "a"), (0.11, "b"), (0.30, "c")]])
        self.assertEqual(len(centers), 2)

    def test_empty(self):
        self.assertEqual(cluster_columns([]), [])


def _page(rows):
    """构造一页 (x, 文本) 的行，取材自真实资产负债表。

    真实坐标（取自苏州井利电子 2024 年报第 4 页）：

        科目名左对齐   x≈0.177
        合计行右对齐   x≈0.271     ← 同一个视觉列，x 差很远
        行次           x≈0.485
        期末余额       x≈0.589
        上年年末余额   x≈0.752
    """
    return [[(0.177, "货币资金"), (0.485, "1"), (0.589, "6,840,705.08"), (0.752, "6,658,401.03")],
            [(0.177, "应收账款"), (0.485, "3"), (0.589, "47,373,405.86"), (0.752, "61,996,794.23")],
            [(0.271, "流动资产合计"), (0.485, "14"), (0.589, "78,593,867.51"), (0.752, "109,460,973.80")],
            [(0.288, "资产总计"), (0.485, "34"), (0.589, "150,066,440.63"), (0.752, "202,275,237.27")]] + rows


class TestRowsToTable(unittest.TestCase):
    """OCR 行 → 表格。这一步做不对，金额就会落到错误的栏。"""

    def test_right_aligned_total_keeps_its_label(self):
        """**合计行的标签右对齐，不能因此丢掉。**

        中文财报里普通科目名左对齐（x≈0.177），合计行右对齐（x≈0.27）。
        按 x 聚类会分成两列；只取最左列就取空了 —— 于是资产总计、
        负债合计全变成「没有标签」，**勾稽直接判不了**。

        正确做法：标签 = 第一个数字列**左边的一切**。
        """
        out = rows_to_table(_page([]))
        labels = [r[0] for r in out]
        self.assertIn("流动资产合计", labels)
        self.assertIn("资产总计", labels)

    def test_total_values_land_in_right_columns(self):
        out = rows_to_table(_page([]))
        total = next(r for r in out if r[0] == "资产总计")
        self.assertEqual(total[2], "150,066,440.63")     # 期末
        self.assertEqual(total[3], "202,275,237.27")     # 上年年末

    def test_row_number_is_not_a_value(self):
        """`行次` 是小整数，**绝不能当金额**。"""
        out = rows_to_table(_page([]))
        cash = next(r for r in out if r[0] == "货币资金")
        self.assertEqual(cash[1], "1")                   # 行次单独一列
        self.assertEqual(cash[2], "6,840,705.08")

    def test_suspect_value_is_marked(self):
        """判据不明确的金额带上 `？`，**可见地**留下而不是静默当数用。"""
        rows = _page([[(0.177, "预付款项"), (0.485, "7"), (0.589, "45.508.51239"),
                       (0.752, "10,677,467.06")]])
        out = rows_to_table(rows)
        pre = next(r for r in out if r[0] == "预付款项")
        self.assertEqual(pre[2], "？45.508.51239")

    def test_repaired_value_is_output_in_fixed_form(self):
        """**修好的数必须以修好的形式输出。**

        输出原文的话，下游 `_to_number("334.719.50")` 会剥成 33471950
        —— 差 100 倍且不报错。实测踩到。
        """
        rows = _page([[(0.177, "预付款项"), (0.485, "7"), (0.589, "334.719.50"),
                       (0.752, "10,677,467.06")]])
        out = rows_to_table(rows)
        pre = next(r for r in out if r[0] == "预付款项")
        self.assertEqual(pre[2], "334,719.50")

    def test_empty_input(self):
        self.assertEqual(rows_to_table([]), [])


class TestValueLikeVsNumeric(unittest.TestCase):
    """这两个判断必须分开 —— 合并了 `？` 标记就会丢。"""

    def test_suspect_is_value_like_but_not_numeric(self):
        self.assertTrue(_is_value_like("334.719.50"))
        self.assertFalse(parse_amount("334.719.50")[1] == "ok")

    def test_row_number_is_value_like(self):
        self.assertTrue(_is_value_like("14"))

    def test_text_is_neither(self):
        self.assertFalse(_is_value_like("资产负债表"))


class TestAvailability(unittest.TestCase):
    def test_available_returns_bool(self):
        self.assertIsInstance(available(), bool)


if __name__ == "__main__":
    unittest.main()
