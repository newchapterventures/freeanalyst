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


class TestNwcCrosscheck(unittest.TestCase):
    """净营运资本的「分项 vs 合计」——「分项 > 合计」是算术上不可能的事。

    它比"对不上"更硬：**没有口径差异的余地**。能抓到两类真会发生的错 ——
    量级读错（差三个数量级）、合并表与母公司表串行（实测某 A 股年报
    「其他流动资产」一行出现 6 个取值）。
    """

    @staticmethod
    def _S(ar, inv, ap, cur_a=None, cur_l=None):
        S = _statements([("营业收入", 1000.0)], {Field.REVENUE: 1000.0})
        rows = [("应收账款", ar), ("应付账款", ap)]
        if inv is not None:
            rows.append(("存货", inv))
        if cur_a is not None:
            rows.append(("流动资产合计", cur_a))
        if cur_l is not None:
            rows.append(("流动负债合计", cur_l))
        S.balance = _set("balance", rows, {Field.ACCOUNTS_RECEIVABLE: ar,
                                           Field.ACCOUNTS_PAYABLE: ap,
                                           **({Field.INVENTORY: inv} if inv is not None else {})})
        return S

    def test_item_exceeding_total_is_caught(self):
        """应收账款比流动资产合计还大 —— 一定是哪里错了（量级或串行）。"""
        S = self._S(ar=9999.0, inv=10.0, ap=5.0, cur_a=100.0, cur_l=80.0)
        msg = S.nwc_crosscheck()
        self.assertIn("不自洽", msg)
        self.assertIn("应收账款", msg)
        self.assertIn("请核对", msg)

    def test_payable_exceeding_current_liabilities_is_caught(self):
        S = self._S(ar=10.0, inv=10.0, ap=9999.0, cur_a=100.0, cur_l=80.0)
        msg = S.nwc_crosscheck()
        self.assertIn("不自洽", msg)
        self.assertIn("应付账款", msg)

    def test_consistent_items_report_so(self):
        S = self._S(ar=30.0, inv=20.0, ap=25.0, cur_a=100.0, cur_l=80.0)
        self.assertIn("核对自洽", S.nwc_crosscheck())

    def test_no_total_means_no_claim(self):
        """合计取不到就**什么都不说** —— 不猜、不制造噪声。"""
        S = self._S(ar=30.0, inv=20.0, ap=25.0)
        self.assertEqual(S.nwc_crosscheck(), "")

    def test_notes_show_basis_and_check(self):
        S = self._S(ar=30.0, inv=20.0, ap=25.0, cur_a=100.0, cur_l=80.0)
        S.unit = "千元"
        note = S.history_notes().get("历史净营运资本占收入比", "")
        self.assertIn("依据", note)
        self.assertIn("应收账款", note)
        self.assertIn("应付账款", note)
        self.assertIn("核对自洽", note)


class TestCapexCrosscheck(unittest.TestCase):
    """资本开支只有一个正式来源，所以这里不是"两来源对比"，而是**拿界卡它**。

    界 = 「投资活动现金流出小计」（行标签取）。资本开支是它的组成项，
    超过它在算术上不可能 —— 这种界**没有口径差异的余地**，比"对不上"硬。
    """

    @staticmethod
    def _S(capex, outflow=None):
        S = _statements([("营业收入", 1000.0)], {Field.REVENUE: 1000.0})
        rows = [("购建固定资产、无形资产和其他长期资产支付的现金", capex)]
        if outflow is not None:
            rows.append(("投资活动现金流出小计", outflow))
        S.cash_flow = _set("cash_flow", rows, {Field.CAPEX: capex})
        return S

    def test_capex_exceeding_outflow_is_caught(self):
        S = self._S(capex=9999.0, outflow=1000.0)
        msg = S.capex_crosscheck()
        self.assertIn("算术上不可能", msg)
        self.assertIn("请核对", msg)

    def test_within_bound_reports_consistency(self):
        S = self._S(capex=300.0, outflow=1000.0)
        self.assertIn("核对自洽", S.capex_crosscheck())

    def test_no_bound_means_no_claim(self):
        """界取不到就**什么都不说** —— 不猜、不制造噪声。"""
        self.assertEqual(self._S(capex=300.0).capex_crosscheck(), "")

    def test_notes_carry_basis_and_check(self):
        S = self._S(capex=300.0, outflow=1000.0)
        S.unit = "千元"
        note = S.history_notes().get("历史资本开支占收入比", "")
        self.assertIn("口径", note)
        self.assertIn("依据", note)
        self.assertIn("核对自洽", note)

    def test_negative_outflow_is_shown_as_magnitude(self):
        """现金流量表把流出印成负数是**排版约定**（实测某 10-K 印成 -78,640）。

        照原样显示会让读者以为"资本开支是负的" —— 比率用的是量级，
        提示里也该按量级显示，但要说清原表的印法（不能悄悄改数）。
        """
        S = self._S(capex=-78640.0, outflow=100000.0)
        S.unit = "千美元"
        note = S.history_notes().get("历史资本开支占收入比", "")
        self.assertIn("78,640千美元", note)
        self.assertNotIn("-78,640", note)
        self.assertIn("原表以负数列示流出", note)

    def test_positive_outflow_gets_no_extra_note(self):
        S = self._S(capex=78640.0, outflow=100000.0)
        S.unit = "元"
        note = S.history_notes().get("历史资本开支占收入比", "")
        self.assertIn("78,640元", note)
        self.assertNotIn("负数列示", note)


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
