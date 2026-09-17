"""招股说明书股本结构抽取的测试。

原文片段照抄实测材料 —— **包括那些让第一版把三个数全搞错的写法**。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from financials import shares  # noqa: E402


class _Page:
    def __init__(self, number, text):
        self.number, self.text = number, text


class _Doc:
    def __init__(self, pages):
        self.pages = pages


#: 照抄实测材料第 2 页 —— 标签排在数值**后面**，说明句里也有标签
P1 = _Page(2, (
    "本次发行概况 发行股票类型 人民币普通股(A股) 不超过 217,243,733 股,"
    "占发行后总股本比例不低于发行股数 10.00%。全部为发行新股,不涉及股东公开发售股份 "
    "每股面值 人民币1.00 元 每股发行价格 【】元 预计发行日期 【】年【】月【】日 "
    "发行后总股本不超过2,172,437,000股"))

#: 照抄实测材料第 3 页 —— 单位是「万股」，且「占发行后总股本的比例」是说明句
P2 = _Page(3, (
    "发行概况 发行股票类型 人民币普通股(A股) 本次拟公开发行股份数量不超过2,000万股,"
    "占发行后总股本的比例不低于 25%。本次发行全部为新股发行,原股东不公开发售股份。 "
    "每股面值 1.00元 每股发行价格 [ ]元 拟上市的证券交易所和板块 深圳证券交易所创业板 "
    "不超过8,000万股 发行后总股本"))


class TestShareStructure(unittest.TestCase):
    def test_absolute_and_wan_units(self):
        """绝对数（217,243,733 股）和万字头（2,000 万股）都要认。"""
        s = shares.extract_shares(_Doc([P1]))
        self.assertEqual(s.shares_issued, 217_243_733)
        self.assertEqual(s.shares_after, 2_172_437_000)

        s2 = shares.extract_shares(_Doc([P2]))
        self.assertEqual(s2.shares_issued, 20_000_000)   # 2,000 万股
        self.assertEqual(s2.shares_after, 80_000_000)    # 8,000 万股

    def test_label_after_value(self):
        """**回归：标签排在数值后面时，不能跨到下一个字段。**

        某招股书的文字层是
        `不超过 217,243,733 股,占发行后总股本比例不低于发行股数 10.00%。`
        —— 「发行股数」在说明句里，真值在它**前面**。

        第一版窗口开到 120 字，跨过 `10.00%` 抓到了下一个字段的
        `2,172,437,000股`：发行股数被当成 21.7 亿、发行后总股本也是 21.7 亿、
        发行前股本被推成 0。**三个数全错，而且看起来都挺像样。**
        """
        s = shares.extract_shares(_Doc([P1]))
        self.assertNotEqual(s.shares_issued, s.shares_after,
                            "发行股数不该等于发行后总股本")
        self.assertEqual(s.shares_issued, 217_243_733)

    def test_skip_explanatory_mention(self):
        """**回归：说明句里的标签要跳过。**

        `占发行后总股本的比例不低于 25%` 里也有「发行后总股本」，
        紧挨着的数字却是**发行股数**。第一版因此把 8,000 万股读成 2,000 万股。
        """
        s = shares.extract_shares(_Doc([P2]))
        self.assertEqual(s.shares_after, 80_000_000)

    def test_before_shares_is_derived_and_flagged(self):
        """发行前股本靠推算时必须**标明是推算**。"""
        s = shares.extract_shares(_Doc([P1]))
        self.assertEqual(s.shares_before, 2_172_437_000 - 217_243_733)
        self.assertTrue(any("推算" in n for n in s.notes), s.notes)

    def test_blank_price_says_so(self):
        """**申报稿没定价要明说**，不能当成解析失败、更不能回填。"""
        for doc in (_Doc([P1]), _Doc([P2])):
            s = shares.extract_shares(doc)
            self.assertIsNone(s.offer_price)
            self.assertTrue(any("未定价" in n for n in s.notes), s.notes)

    def test_no_profile_page(self):
        s = shares.extract_shares(_Doc([_Page(1, "资产负债表 货币资金")]))
        self.assertIsNone(s.shares_issued)
        self.assertIn("发行概况", s.render())

    def test_par_value(self):
        self.assertEqual(shares.extract_shares(_Doc([P1])).par_value, 1.0)


class TestHelpers(unittest.TestCase):
    def test_to_shares_wan(self):
        self.assertEqual(shares._to_shares("2,000万股"), 20_000_000)

    def test_to_shares_absolute(self):
        self.assertEqual(shares._to_shares("217,243,733 股"), 217_243_733)


if __name__ == "__main__":
    unittest.main()
