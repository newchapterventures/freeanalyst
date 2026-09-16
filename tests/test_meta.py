"""口径推断的测试。

## 这里测的不是「能不能猜对」，而是「**该不该猜**」

原则：推不出就说不知道。宁可报「未判定」，也不要给一个看起来合理的错标签。
所以下面的用例有一半是**要求它拒答**的。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from financials import meta  # noqa: E402


class TestGaap(unittest.TestCase):
    def test_china_standard(self):
        text = ("货币资金 应收账款 存货 固定资产 应付账款 未分配利润 实收资本 "
                "资产总计 负债合计 所有者权益合计 营业收入 净利润")
        self.assertEqual(meta.detect_gaap(text), "CAS")

    def test_legacy_1993_format(self):
        """1993 年行业会计制度的表号 —— 只有那个年代的表头才有。"""
        text = ("会工01表 会工02表 资产总计 负债合计 所有者权益合计 "
                "产品销售收入 产品销售成本")
        self.assertIn("1993", meta.detect_gaap(text))

    def test_modern_long_term_prepaid_not_legacy(self):
        """**假匹配回归** —— `长期待摊费用` 里含 `待摊费用`。

        第一版的 1993 标志里放了 `待摊费用` / `预提费用`，结果**茅台**
        被判成 1993 年格式：现代资产负债表的「长期待摊费用」直接命中。
        子串匹配在这种地方必须极端小心。
        """
        modern = ("货币资金 应收账款 存货 固定资产 长期待摊费用 递延所得税资产 "
                  "资产总计 负债合计 所有者权益合计 营业收入 净利润")
        self.assertEqual(meta.detect_gaap(modern), "CAS")

    def test_us_gaap(self):
        text = ("Total Current Assets Client Fees Receivables Work in Progress "
                "Retainage Receivable Stockholders Equity Common Stock "
                "PTO Payable Accumulated Deficit Balance Sheet")
        self.assertEqual(meta.detect_gaap(text), "US GAAP")

    def test_us_gaap_from_income_statement_only(self):
        """只有利润表也要能判出来。

        第一版的美国特征词全是资产负债表科目，导致 Cicero 的**利润表**
        单独交进来时判成「未判定」。
        """
        text = ("Total Revenues Cost of Sales Gross Profit Operating Income "
                "Net Income Income Before Taxes Income Statement")
        self.assertEqual(meta.detect_gaap(text), "US GAAP")

    def test_ifrs(self):
        text = ("Non-current assets Property, plant and equipment "
                "Statement of Financial Position Statement of Profit or Loss "
                "Profit for the year Finance costs")
        self.assertEqual(meta.detect_gaap(text), "IFRS")

    def test_bilingual_hk_prefers_china(self):
        """中英双语材料先按中国准则判。

        H 股年报里 IFRS 特征词也有一堆，但它首先是中国公司的报表 ——
        判成 IFRS 会更误导。
        """
        text = ("合并资产负债表 货币资金 应收账款 资产总计 负债合计 所有者权益合计 "
                "Profit for the year Finance costs Non-current assets")
        self.assertEqual(meta.detect_gaap(text), "CAS")

    def test_refuses_to_guess_english_without_markers(self):
        """英文材料但特征不足 —— **必须拒答**。"""
        text = "Assets Liabilities Revenue Cost Profit Loss Depreciation"
        got = meta.detect_gaap(text)
        self.assertTrue(got.startswith("未判定"), got)

    def test_refuses_on_empty(self):
        self.assertEqual(meta.detect_gaap(""), "未判定")


class TestScope(unittest.TestCase):
    def test_consolidated(self):
        self.assertEqual(meta.detect_scope("合并资产负债表 合并利润表"), "合并")

    def test_parent_only(self):
        self.assertEqual(meta.detect_scope("母公司资产负债表 母公司利润表"),
                         "母公司报表")

    def test_single_entity_default(self):
        """非上市审计报告里根本不会出现「合并」或「母公司」——

        这时「单体」是**唯一可能**，不是猜。
        """
        self.assertEqual(
            meta.detect_scope("资产总计 负债合计 所有者权益合计 营业收入"), "单体")

    def test_consolidated_beats_parent(self):
        """两套都在时，出现「合并」字样就算合并报表。"""
        self.assertEqual(
            meta.detect_scope("合并资产负债表 母公司资产负债表"), "合并")

    def test_refuses_on_empty(self):
        self.assertEqual(meta.detect_scope(""), "未判定")


if __name__ == "__main__":
    unittest.main()
