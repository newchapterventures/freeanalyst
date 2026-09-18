"""文字流抽表的测试。

这里的用例全部照抄**实测材料的原文**——尤其是那些让第一版静默出错的写法。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from financials.textflow import parse_textflow  # noqa: E402

#: 用真实科目名当词表（`known_names()` 要 import canonical，这里给最小集）
NAMES = ["流动资产合计", "货币资金", "应收账款", "存货", "长期股权投资",
         "资产总计", "营业收入", "营业成本", "净利润", "固定资产"]

#: 利润表下半段的科目名 —— 带括号说明的那些（见 `TestLabelPunctuation`）
NAMES_IS = NAMES + ["营业利润", "利润总额", "所得税费用"]


class TestAmountCount(unittest.TestCase):
    def test_two_periods(self):
        rows = parse_textflow("货币资金 1,000.00 900.00", NAMES)
        self.assertEqual(rows[0][2], "1,000.00")
        self.assertEqual(rows[0][3], "900.00")

    def test_four_periods_takes_newest_first(self):
        """**回归：四期报表不能取到最旧的两期。**

        招股说明书的列是**四期、由新到旧**：

            项目            2022年6月30日     2021-12-31      2020-12-31      2019-12-31
            流动资产合计     481,320,670.39   613,757,717.99  550,181,213.87  489,225,455.77

        原实现最多吃 3 个金额、且取**最后两个** —— 四个期间被截断，
        取到的是 **2020/2019**（最旧的两期），而不是 2022H1/2021。
        **静默取错期间**：数字本身挺像样，任何勾稽校验都抓不住。
        """
        line = ("流动资产合计 481,320,670.39 613,757,717.99 "
                "550,181,213.87 489,225,455.77")
        rows = parse_textflow(line, NAMES)
        self.assertEqual(rows[0][2], "481,320,670.39", "主值必须是最新一期")
        self.assertEqual(rows[0][3], "613,757,717.99", "次值必须是上一期")

    def test_row_number_column_stripped(self):
        """带「行次」的 A 股年报：`科目名 行次 期末 期初` 三个数。

        行次要被识别并剥掉，两个金额都保留。
        """
        rows = parse_textflow("货币资金 1 329,320,880.74 285,000,000.00", NAMES)
        self.assertEqual(rows[0][1], "1", "行次应进 note")
        self.assertEqual(rows[0][2], "329,320,880.74", "期末值不能丢")
        self.assertEqual(rows[0][3], "285,000,000.00")

    def test_single_value_row(self):
        rows = parse_textflow("存货 500.00", NAMES)
        self.assertEqual(rows[0][2], "500.00")
        self.assertEqual(rows[0][3], "")

    def test_dash_placeholder(self):
        """破折号表示「零 / 无」，不是缺失。"""
        rows = parse_textflow("长期股权投资 -", NAMES)
        self.assertEqual(rows[0][2], "-")


class TestLabelStripping(unittest.TestCase):
    def test_concatenated_labels(self):
        """没有金额的科目会连成一片，只有最后一个带数。

            结算备付金 拆出资金 交易性金融资产 1,189,982,759.28

        这类在金融业资产负债表里很常见。
        """
        names = NAMES + ["拆出资金", "交易性金融资产"]
        rows = parse_textflow("拆出资金交易性金融资产 1,189,982,759.28", names)
        labels = [r[0] for r in rows]
        self.assertIn("交易性金融资产", labels)
        got = next(r for r in rows if r[0] == "交易性金融资产")
        self.assertEqual(got[2], "1,189,982,759.28")

    def test_unknown_label_dropped(self):
        """科目名认不出来就丢掉 —— **不许造一个假科目**。"""
        rows = parse_textflow("这根本不是科目 123.45", NAMES)
        self.assertEqual(rows, [])


class TestLabelPunctuation(unittest.TestCase):
    """**科目名里的括号说明不能把标签切断。**

    实测原始文本（某 A 股年报第 75 页，`“` `”` 是弯引号 U+201C/U+201D）：

        三、营业利润(亏损以“-”号填列) 284,992,517.86 -286,489,949.67
        四、利润总额(亏损总额以“-”号填 286,137,806.23 -276,696,267.60 列)

    引号、连字符、冒号原先都不在 `_PIECE` 的字符类里 —— 匹配在「亏损以」
    后面断掉，然后从「号填列)」重新开始，**真科目名丢了**。
    `strip_names` 认不出「号填列」这种残渣，整行被**静默丢掉**。

    代价：`营业利润` 和 `利润总额` 两行全丢 → EBITDA 和实际税率都算不出来。
    另一份材料的完整标签 `三、营业利润(亏损以“-”号填列)` 反而能过 ——
    所以这个 bug 只在**换行把标签切断时**才发作，非常隐蔽。
    """

    QUOTED = [
        "三、营业利润(亏损以“-”号填列) 284,992,517.86 -286,489,949.67",
        # ↓ 标签被换行切断，结尾的「列)」跑到了数字后面
        "四、利润总额(亏损总额以“-”号填 286,137,806.23 -276,696,267.60 列)",
        "减：所得税费用 63,303,783.82 20,245,213.77",
        "其中：归属于母公司股东的净利润 224,665,777.93 -290,675,602.27",
    ]

    def test_operating_income_survives_quotes(self):
        rows = parse_textflow(self.QUOTED[0], NAMES_IS)
        self.assertIn("营业利润", [r[0] for r in rows])
        got = next(r for r in rows if r[0] == "营业利润")
        self.assertEqual(got[2], "284,992,517.86")

    def test_pretax_income_survives_truncated_label(self):
        """标签被切断也要认出来 —— 靠 `strip_names` 从右往左剥。"""
        rows = parse_textflow(self.QUOTED[1], NAMES_IS)
        labels = [r[0] for r in rows]
        self.assertIn("利润总额", labels)
        got = next(r for r in rows if r[0] == "利润总额")
        self.assertEqual(got[2], "286,137,806.23")

    def test_fullwidth_colon(self):
        rows = parse_textflow(self.QUOTED[2], NAMES_IS)
        got = next((r for r in rows if r[0] == "所得税费用"), None)
        self.assertIsNotNone(got, "全角冒号不能切断标签")
        self.assertEqual(got[2], "63,303,783.82")

    def test_negative_without_space(self):
        """**标签后面直接跟负数不能被吃掉负号。**

        字符类里放进连字符以后，`营业利润 -286` 有可能被贪婪匹配成
        `营业利润-` + `286`。`strip_names` 从右往左剥能兜住，这里锁住它。
        """
        rows = parse_textflow("营业利润 -286,489,949.67 100.00", NAMES_IS)
        got = next(r for r in rows if r[0] == "营业利润")
        self.assertEqual(got[2], "-286,489,949.67")

    def test_original_bug_would_have_dropped_the_row(self):
        """锁住「修之前会怎样」—— 防的是一直没意识到这个问题又改回去。"""
        before = (r"([\u4e00-\u9fff][\u4e00-\u9fff（）()、·]*)")
        self.assertNotIn("“", before, "旧字符类里没有引号 —— 这正是 bug")


if __name__ == "__main__":
    unittest.main()
