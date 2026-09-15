"""科目映射 + 勾稽校验测试。

材料是 **Fitbit FY2016 10-K 的真实申报文件**（SEC 公开数据）。
用真实材料是因为合成样例测不出这些：

- 真实表里有分节标题、小计行、多列年份
- 真实数字有括号负数、有汇率影响这种"额外一行"

## 这个文件里最重要的两类用例

**① 该平的时候必须平。** 三条勾稽关系都要对得上。
**② 该不平的时候必须不平。** 把某个字段改错，校验必须抓到 ——
   否则这个"验收标准"是假的，平不平都通过等于没做。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import articulation as art  # noqa: E402
from financials import canonical as cn  # noqa: E402
from financials.canonical import Field  # noqa: E402
from ingest import html as ih  # noqa: E402

FITBIT = ROOT / "materials" / "fitbit-2016-10k"


def load(rfile: str):
    """抽一张表 → (行列表, 字段字典)。取第一列（当期）。"""
    t = ih.extract_html(FITBIT / rfile).tables[0]
    rows, fields = [], {}
    for r in t.rows:
        if not r.cells:
            continue
        vals = r.numeric_cells()
        v = vals[0] if vals else None
        f, _ = cn.identify(r.label, r.xbrl_tag)
        rows.append((r.label, v, f))
        if f and f not in fields and v is not None:
            fields[f] = v
    return rows, fields


class TestIdentify(unittest.TestCase):
    """科目识别：XBRL 标签优先，行名兜底。"""

    def test_priority_is_xbrl_tag(self):
        """标签是官方确定答案，优先于行名。"""
        f, via = cn.identify("随便什么行名", "us-gaap:Assets")
        self.assertEqual(f, Field.TOTAL_ASSETS)
        self.assertEqual(via, "tag")

    def test_chinese_name_matching(self):
        self.assertEqual(cn.identify("货币资金")[0], Field.CASH)
        self.assertEqual(cn.identify("营业总收入")[0], Field.REVENUE)
        self.assertEqual(cn.identify("应收账款净额")[0], Field.ACCOUNTS_RECEIVABLE)

    def test_whitespace_and_punctuation_insensitive(self):
        self.assertEqual(cn.identify(" 货币 资金 ")[0], Field.CASH)
        self.assertEqual(cn.identify("（货币资金）")[0], Field.CASH)

    def test_abstract_tags_are_section_headers(self):
        """`XXXAbstract` 是分节标题，不是金额行。"""
        f, _ = cn.identify("Current assets:", "us-gaap:AssetsCurrentAbstract")
        self.assertEqual(f, Field.SECTION)

    def test_unknown_returns_none(self):
        self.assertEqual(cn.identify("某公司自创的奇怪科目")[0], None)

    def test_ambiguous_cash_tag_is_disambiguated_by_label(self):
        """**同一个标签在两张表里含义不同。**

        `CashAndCashEquivalentsAtCarryingValue` 在资产负债表里是期末余额，
        在现金流量表里可能是**期初**余额。光看标签会把期初当期末，
        现金勾稽直接对不上。
        """
        self.assertEqual(
            cn.identify("Cash and cash equivalents at beginning of period",
                        "us-gaap:CashAndCashEquivalentsAtCarryingValue")[0],
            Field.CASH_BEGIN)
        self.assertEqual(
            cn.identify("Cash and cash equivalents at end of period",
                        "us-gaap:CashAndCashEquivalentsAtCarryingValue")[0],
            Field.CASH_END)


@unittest.skipUnless((FITBIT / "R2.htm").exists(), "需要 Fitbit 10-K 材料")
class TestRealFitbit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bal_rows, cls.bal = load("R2.htm")
        cls.cf_rows, cls.cf = load("R8.htm")

    # ---------- 该平的时候必须平 ----------

    def test_balance_sheet_balances(self):
        r = art.check_balance(self.bal)
        self.assertTrue(r.ok, f"资产负债不平：{r.render()}")

    def test_cash_rollforward_balances(self):
        r = art.check_cash_rollforward(
            self.cf, self.cf.get(Field.CASH_BEGIN), self.cf.get(Field.CASH_END))
        self.assertTrue(r.ok, f"现金勾稽不平：{r.render()}")
        self.assertIn("汇率影响", r.note,
                      "汇率影响那行漏了 —— 实测就是漏它导致差 -374")

    def test_indirect_method_balances(self):
        start = end_i = None
        for i, (_, _, f) in enumerate(self.cf_rows):
            if f == Field.NET_INCOME and start is None:
                start = i
            if f == Field.CFO and start is not None:
                end_i = i
                break
        self.assertIsNotNone(start, "定位不到净利润行")
        self.assertIsNotNone(end_i, "定位不到经营现金流行")
        between = [(l, v) for l, v, _ in self.cf_rows[start + 1:end_i]]
        r = art.check_indirect_method(
            between, self.cf.get(Field.NET_INCOME), self.cf.get(Field.CFO))
        self.assertTrue(r.ok, f"间接法不平：{r.render()}")

    def test_key_totals_match_the_filing(self):
        """手抄自年报原文，核对映射没取错行。"""
        self.assertEqual(self.bal[Field.TOTAL_ASSETS], 1820226)
        self.assertEqual(self.bal[Field.TOTAL_LIABILITIES], 821694)
        self.assertEqual(self.bal[Field.EQUITY], 998532)

    # ---------- 该不平的时候必须不平 ----------

    def test_wrong_asset_caught(self):
        bad = dict(self.bal)
        bad[Field.TOTAL_ASSETS] = 1820226 + 5000
        r = art.check_balance(bad)
        self.assertFalse(r.ok, "改错了 5,000 却没抓到 —— 这个校验是假的")
        self.assertAlmostEqual(r.diff, 5000, delta=1)

    def test_wrong_equity_caught(self):
        bad = dict(self.bal)
        bad[Field.EQUITY] = 998532 - 1
        r = art.check_balance(bad)
        # 差 1 在容差内（容忍报表四舍五入），不该报错
        self.assertTrue(r.ok)

    def test_missing_field_is_not_a_failure(self):
        """**缺数据不是失败，是判不了。** 两者必须分开 ——
        把"缺"当"失败"会让人去修本来没问题的映射。"""
        r = art.check_balance({Field.TOTAL_ASSETS: 100.0})
        self.assertIsNone(r.ok)
        self.assertIn(Field.TOTAL_LIABILITIES, r.missing)
        self.assertIn("数据不足", r.status)

    def test_cash_mismatch_caught(self):
        bad = dict(self.cf)
        bad[Field.CFO] = bad[Field.CFO] + 10000
        r = art.check_cash_rollforward(bad, bad.get(Field.CASH_BEGIN),
                                       bad.get(Field.CASH_END))
        self.assertFalse(r.ok, "改错了经营现金流却没抓到")


class TestIndirectMethodRules(unittest.TestCase):
    """间接法加总的两条规则。"""

    def test_sums_the_block(self):
        rows = [("Depreciation", 100.0), ("Stock-based comp", 50.0)]
        r = art.check_indirect_method(rows, 1000.0, 1150.0)
        self.assertTrue(r.ok)

    def test_skips_totalish_rows(self):
        """**小计行要跳过，否则一段加两遍。**"""
        rows = [("Depreciation", 100.0), ("Stock-based comp", 50.0),
                ("Total adjustments", 150.0)]
        r = art.check_indirect_method(rows, 1000.0, 1150.0)
        self.assertTrue(r.ok, "小计行被重复计入，和变成 1300")
        self.assertIn("跳过汇总行", r.note)

    def test_none_values_ignored(self):
        rows = [("Depreciation", 100.0), ("表头", None), ("Stock-based comp", 50.0)]
        r = art.check_indirect_method(rows, 1000.0, 1150.0)
        self.assertTrue(r.ok)


if __name__ == "__main__":
    unittest.main()
