"""经营利润的**反算复核** —— 用利润表其他行把同一个数推出来，两条路对一对。

## 为什么
实测某港股年报的 EBITDA 率是 **−234.75%**，看着像取数错了。用反算验：
`毛利 + 其他收入 + 其他亏损净额 − 销售 − 管理 − 研发 = −235.68%`，
与取到的经营亏损 −235.67% **分毫不差** → 不是取错，是这一年真的巨亏。
**不需要真值、不需要外部数据** —— 这是这条路的全部价值（所以扫描件上也能用）。

## 三条边界（都是设计决定，不是实现细节）
1. **按行标签反算，刻意不走科目映射** —— 用同一套映射去验等于自证清白：
   映射错了照样"验证通过"。
2. **两种印法都要认**：A 股把费用印成正数（要减），港交所/IFRS 印成负数（已带符号）。
3. **对不上时不判谁对**，只报"差多少、差额像哪一行"；归不上任何行就明说
   "未经交叉验证" —— 工具唯一的判断力来源是材料自己的算术，
   算术对不上时它就没有依据了。**绝不静默选一个、绝不取平均、绝不丢掉对不上的那个。**
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import statements as stm                    # noqa: E402
from financials.canonical import Field                      # noqa: E402


def _set(name: str, rows: list[tuple[str, float]], fields: dict | None = None):
    s = stm.StatementSet(name=name, source="test")
    s.rows = [stm.StatementRow(label=l, value=v, field=None, via="test") for l, v in rows]
    s.fields = fields or {}
    return s


def _statements(inc_rows, inc_fields, extra_rows=(), unit="元"):
    S = stm.Statements(gaap="", scope="", audited="", period="", unit=unit)
    S.income = _set("income", inc_rows, inc_fields)
    if extra_rows:
        S.cash_flow = _set("cash_flow", list(extra_rows))
    return S


# 港交所印法：费用本身带负号
_HK_ROWS = [
    ("Revenue 收入", 1000.0),
    ("Gross Profit 毛利", 455.0),
    ("Other revenue 其他收入", 165.0),
    ("Other net loss 其他虧損淨額", -171.0),
    ("Selling and marketing expenses 銷售及營銷開支", -1345.0),
    ("General and administration expenses 一般及行政開支", -1217.0),
    ("Research and development expenses 研發開支", -243.0),
    ("Loss from operations 經營虧損", -2356.0),
]
# A 股印法：费用是正数绝对额
_CN_ROWS = [
    ("营业收入", 1000.0),
    ("毛利", 455.0),
    ("其他收入", 165.0),
    ("其他亏损净额", -171.0),
    ("销售费用", 1345.0),
    ("管理费用", 1217.0),
    ("研发费用", 243.0),
    ("营业利润", -2356.0),
]


class TestDeriveOperatingIncome(unittest.TestCase):
    def test_hk_signed_expenses(self):
        S = _statements(_HK_ROWS, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2356.0})
        derived, used = S.derive_operating_income()
        self.assertAlmostEqual(derived, -2356.0, places=3)
        self.assertIn("毛利", used)

    def test_cn_absolute_expenses(self):
        """A 股把费用印成正数 —— 判据是费用行的符号，不写死某一家的格式。"""
        S = _statements(_CN_ROWS, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2356.0})
        derived, _used = S.derive_operating_income()
        self.assertAlmostEqual(derived, -2356.0, places=3)

    def test_not_derivable_is_silent(self):
        """反算不出来就**什么都不说** —— 不猜、不制造噪声。"""
        S = _statements([("营业收入", 1000.0)], {Field.REVENUE: 1000.0})
        self.assertEqual(S.derive_operating_income(), (None, []))
        self.assertEqual(S.operating_income_crosscheck(), "")


class TestCrosscheck(unittest.TestCase):
    def test_agreement_is_reported(self):
        S = _statements(_HK_ROWS, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2356.0})
        msg = S.operating_income_crosscheck()
        self.assertIn("一致", msg)
        self.assertIn("互相印证", msg)

    def test_disagreement_attributes_to_a_row(self):
        """**差额自己指认嫌疑人**：差额恰好等于某一行时，直接点名那一行。"""
        rows = list(_HK_ROWS)
        rows[-1] = ("Loss from operations 經營虧損", -2185.0)   # 少算 171
        S = _statements(rows, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2185.0})
        msg = S.operating_income_crosscheck()
        self.assertIn("不一致", msg)
        self.assertIn("其他虧損淨額", msg)
        self.assertIn("请确认", msg)

    def test_disagreement_without_attribution_says_so(self):
        """归不上任何一行 → 明说"未经交叉验证"，**不许**猜一个理由。"""
        rows = list(_HK_ROWS)
        rows[-1] = ("Loss from operations 經營虧損", -1999.0)   # 差 357，表里没有这一行
        S = _statements(rows, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -1999.0})
        msg = S.operating_income_crosscheck()
        self.assertIn("不一致", msg)
        self.assertIn("未经交叉验证", msg)

    def test_rounding_is_not_a_disagreement(self):
        """舍入不算不一致 —— 否则每份材料都会报警（会误报的校验比没有校验更坏）。"""
        rows = list(_HK_ROWS)
        rows[-1] = ("Loss from operations 經營虧損", -2356.4)
        S = _statements(rows, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2356.4})
        self.assertIn("一致", S.operating_income_crosscheck())

    def test_gap_can_point_outside_the_income_statement(self):
        """闯祸的行未必在利润表里 —— 三张表都要扫。"""
        rows = list(_HK_ROWS)
        rows[-1] = ("Loss from operations 經營虧損", -2185.0)
        S = _statements(rows, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2185.0},
                        extra_rows=[("财务费用", 171.0)])
        msg = S.operating_income_crosscheck()
        self.assertIn("不一致", msg)
        self.assertIn("吻合", msg)


class TestNotesCarryTheBasis(unittest.TestCase):
    def test_ebitda_note_shows_basis_and_check(self):
        """**极端值只有把依据摆出来才可解释** —— 这是这一轮真正要交付的东西。"""
        from financials import notes as nt
        S = _statements(_HK_ROWS, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2356.0})
        S.da = nt.DepreciationAmortisation(components={"固定资产折旧": 9.2}, pages=[72])
        note = S.history_notes().get("历史 EBITDA 率", "")
        self.assertIn("依据", note)
        self.assertIn("营业利润", note)
        self.assertIn("折旧摊销", note)
        self.assertIn("一致", note)

    def test_no_da_means_no_basis_line_but_check_still_shown(self):
        """没有 D&A 时**不许编**一句依据 —— 但反算结论照给。"""
        S = _statements(_HK_ROWS, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2356.0})
        note = S.history_notes().get("历史 EBITDA 率", "")
        self.assertNotIn("依据", note)
        self.assertIn("一致", note)


if __name__ == "__main__":
    unittest.main()
