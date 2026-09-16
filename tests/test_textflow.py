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


if __name__ == "__main__":
    unittest.main()
