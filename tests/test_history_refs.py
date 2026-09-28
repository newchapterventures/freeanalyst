"""四个历史比率参考值：**该给的要给出来，给不出来要说清为什么**。

背景（用户 2026-09-24 的提问）：界面上四个比率全显示「数据不足」，他问
"以上指标的参考数无法从财报中读出吗"。查下来是三件事混在一起：

  ① **确实能读** —— 折旧摊销、资本开支在现金流量表里，营运资本在资产负债表里 ✓
     实测 Fitbit：1.76% / 3.62% / 18.18% / -3.43% 全出来了
  ② **写法没覆盖** —— 港交所年报写「購置物業、廠房及設備」，行名还是"英文+繁体"且被截断 ✗
     → 补了包含/前缀规则后，宝宝树年报的资本开支从"取不到"变成 2.09%
  ③ **数字抽不到** —— 扫描件（OCR）里科目名认得出、数值取不到 ✗
     这是取数问题，不该跟"财报里没有"混为一谈

所以本测试钉三件事：写法覆盖（含**不许误伤**的反例）、营运资本放宽、
以及「数据不足」必须带原因。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import cli as fc                          # noqa: E402
from financials import statements as stm                  # noqa: E402
from financials.canonical import Field, identify          # noqa: E402

FITBIT = ROOT / "materials" / "fitbit-2016-10k"


class TestLabelCoverage(unittest.TestCase):
    """真实年报里的写法必须认得出 —— 附**反例**，防止为了覆盖而误伤。"""

    def test_da_lines_in_the_indirect_method(self):
        for label in ("固定资产折旧、油气资产折耗、生产性生物资产折旧",
                      "固定资产折旧", "使用权资产折旧", "投资性房地产折旧",
                      "无形资产摊销", "长期待摊费用摊销", "折旧及摊销",
                      "Depreciation and amortisation 折舊及攤銷"):
            f, via = identify(label)
            self.assertEqual(f, Field.ID_DA, f"{label} → {f}（依据 {via}）")

    def test_accumulated_depreciation_is_not_a_period_expense(self):
        """**累计折旧／累计摊销是资产负债表上的抵减项，不是当期费用。**

        把它算进 EBITDA，等于把存量数字混进当期流量 —— 这类错最难发现，
        因为它不会让勾稽不平。
        """
        for label in ("累计折旧", "减：累计折旧", "累计摊销", "Accumulated depreciation"):
            f, _via = identify(label)
            self.assertNotEqual(f, Field.ID_DA, f"{label} 被当成当期折旧了")

    def test_capex_written_in_the_ways_real_filings_use(self):
        for label in ("购建固定资产、无形资产和其他长期资产支付的现金",
                      "购建固定资产支付的现金", "购置固定资产支付的现金",
                      "購置物業、廠房及設備",
                      "Payments for purchase of property, plant 購置物…（截断）",
                      "购建油气资产支付的现金"):
            f, via = identify(label)
            self.assertEqual(f, Field.CAPEX, f"{label} → {f}（依据 {via}）")

    def test_disposal_proceeds_are_not_capex(self):
        """卖资产收到的是**处置回款**，不是资本开支 —— 覆盖规则最容易在这里误伤。"""
        for label in ("出售物業、廠房及設備所得款項", "处置固定资产收回的现金净额",
                      "Proceeds from sale of property, plant and equipment"):
            f, _via = identify(label)
            self.assertNotEqual(f, Field.CAPEX, f"{label} 被当成资本开支了")

    def test_hk_operating_profit(self):
        for label in ("經營溢利", "经营溢利", "Operating profit"):
            f, _via = identify(label)
            self.assertEqual(f, Field.OPERATING_INCOME, label)


def _stmt(rows, unit="千美元"):
    """按 `load_one` 的口径造一张表：有值才进 fields（与真实装载一致）。"""
    s = stm.StatementSet(name="t", source="t", unit=unit, n_tables=1)
    for label, value, field in rows:
        s.rows.append(stm.StatementRow(label, value, field, "test"))
        if field and field not in s.fields and value is not None:
            s.fields[field] = value
    return s


def _set(revenue=1000.0, oi=100.0, da=50.0, capex=-30.0,
         ar=200.0, inv=80.0, ap=120.0, *, drop=()):
    """一套最小但完整的报表集。`drop` 用来模拟某个科目缺失。"""
    if "revenue" not in drop:
        inc = _stmt([("营业收入", revenue, Field.REVENUE)])
    else:
        inc = _stmt([])
    if "oi" not in drop:
        inc.rows.append(stm.StatementRow("营业利润", oi, Field.OPERATING_INCOME, "test"))
        inc.fields[Field.OPERATING_INCOME] = oi
    if "da" not in drop:
        inc.rows.append(stm.StatementRow("折旧与摊销", da, Field.DEPRECIATION_AMORTIZATION,
                                         "test"))
        inc.fields[Field.DEPRECIATION_AMORTIZATION] = da
    cf = _stmt([])
    if "capex" not in drop:
        cf.rows.append(stm.StatementRow("购建固定资产支付的现金", capex, Field.CAPEX, "test"))
        cf.fields[Field.CAPEX] = capex
    bal = _stmt([])
    for key, val, field in (("ar", ar, Field.ACCOUNTS_RECEIVABLE),
                            ("inv", inv, Field.INVENTORY),
                            ("ap", ap, Field.ACCOUNTS_PAYABLE)):
        if key not in drop:
            bal.rows.append(stm.StatementRow(field.value, val, field, "test"))
            bal.fields[field] = val
    s = stm.Statements(balance=bal, income=inc, cash_flow=cf, unit="千美元")
    return s


class TestHistoryRatios(unittest.TestCase):
    def test_all_four_compute_when_the_accounts_are_there(self):
        h = _set().history()
        self.assertAlmostEqual(h["历史折旧摊销占收入比"], 0.05)
        self.assertAlmostEqual(h["历史资本开支占收入比"], 0.03, msg="资本开支要取绝对值")
        self.assertAlmostEqual(h["历史净营运资本占收入比"], (200 + 80 - 120) / 1000)
        self.assertAlmostEqual(h["历史 EBITDA 率"], (100 + 50) / 1000)

    def test_nwc_survives_a_missing_inventory(self):
        """**没有存货科目不是"数据不足"。** 服务业常见 —— 应收减应付就是营运资本主干。

        原来要求"应收+存货+应付"三个都在，等于把没有存货的公司整块判成缺数。
        """
        h = _set(drop=("inv",)).history()
        self.assertAlmostEqual(h["历史净营运资本占收入比"], (200 - 120) / 1000)
        notes = _set(drop=("inv",)).history_notes()
        self.assertIn("未含存货", notes["历史净营运资本占收入比"], "放宽了但要说清")

    def test_nwc_still_needs_receivables_and_payables(self):
        for miss in ("ar", "ap"):
            h = _set(drop=(miss,)).history()
            self.assertIsNone(h["历史净营运资本占收入比"], f"缺 {miss} 不该给出数")

    def test_da_read_from_either_field_name(self):
        """同一行可能被判成 `ID_DA` 或 `折旧与摊销` —— 两种都要算进去。"""
        cf = _stmt([("折旧与摊销", 40.0, Field.DEPRECIATION_AMORTIZATION),
                    ("无形资产摊销", 10.0, Field.ID_DA)])
        inc = _stmt([("营业收入", 1000.0, Field.REVENUE)])
        s = stm.Statements(balance=_stmt([]), income=inc, cash_flow=cf, unit="")
        self.assertAlmostEqual(s.da_total(), 50.0)


class TestReasonsInsteadOfBareNotEnough(unittest.TestCase):
    """「数据不足」四个字不够 —— 三种成因的应对完全不同，必须说出来。"""

    def test_says_why_da_is_missing(self):
        r = _set(drop=("da",)).history_reasons()
        msg = r["历史折旧摊销占收入比"]
        self.assertTrue(msg, "缺折旧摊销必须给原因")
        # 正式措辞，不用"没找到/没认出来"这类口语
        self.assertTrue(any(k in msg for k in ("没有", "未取到", "未识别")), msg)

    def test_says_which_nwc_part_is_missing(self):
        r = _set(drop=("ap",)).history_reasons()
        self.assertIn("应付账款", r["历史净营运资本占收入比"])

    def test_no_cash_flow_is_named_as_such(self):
        s = _set()
        s.cash_flow = None
        r = s.history_reasons()
        self.assertIn("现金流量表", r["历史资本开支占收入比"])

    def test_recognized_but_valueless_row_is_a_different_reason(self):
        """**科目名已识别、但未取到数值** —— 这是扫描件的常态，与"缺少该科目行"要分开说。"""
        cf = _stmt([])
        cf.rows.append(stm.StatementRow("购建固定资产支付的现金", None, Field.CAPEX, "test"))
        s = _set()
        s.cash_flow = cf
        r = s.history_reasons()
        self.assertIn("未取到数值", r["历史资本开支占收入比"])


@unittest.skipUnless((FITBIT / "R2.htm").exists(), "需要 Fitbit 10-K 材料")
class TestFitbitRegression(unittest.TestCase):
    """真实材料的回归 —— 这四个数在 Fitbit 上是算得出来的，改动不许把它们弄丢。"""

    def test_four_ratios_still_compute(self):
        cfg = {"statements": {
            "unit": "千美元", "as_of": "2016-12-31", "gaap": "US GAAP",
            "scope": "合并", "audited": "已审计",
            "balance_sheet": "materials/fitbit-2016-10k/R2.htm",
            "income_statement": "materials/fitbit-2016-10k/R4.htm",
            "cash_flow": "materials/fitbit-2016-10k/R8.htm"}}
        h = fc.load_from_config(cfg, ROOT).history()
        self.assertAlmostEqual(h["历史折旧摊销占收入比"], 0.0176, places=4)
        self.assertAlmostEqual(h["历史资本开支占收入比"], 0.0362, places=4)
        self.assertAlmostEqual(h["历史净营运资本占收入比"], 0.1818, places=4)
        self.assertAlmostEqual(h["历史 EBITDA 率"], -0.0343, places=4)
        self.assertFalse(fc.load_from_config(cfg, ROOT).history_reasons(),
                         "四个都算出来了，就不该有「数据不足」的理由")


class TestDaFromNotes(unittest.TestCase):
    """折旧摊销的三处来源与优先级 —— 这条链断过两次，每次的表现都是"比率空着"。

    实测（某 104 页扫描件）：
      ① 它是**直接法**现金流量表 —— 表里根本没有 D&A 行 ✗
      ② D&A 只在附注的「现金流量表补充资料」里（第 72 页页脚起头、明细在第 73 页）
      ③ 抽出来了、也挂到了 `Statements.da` 上 —— 但**没有任何地方读它** ✗
    三处任何一环断了，EBITDA 率与折旧摊销占收入比就都是空的，而且报告只会说
    "现金流量表中没有折旧摊销科目行"，听起来像材料的问题，其实是我们的问题。
    """

    @staticmethod
    def _da(total):
        class _DA:
            components = {"固定资产折旧": total}
            def __init__(self, t): self.total = t
        return _DA(total)

    def test_notes_total_wins_over_partial_income_statement_line(self):
        """补充资料的总额优先于利润表那一行 —— 后者可能只是计入费用的部分。"""
        s = _set()
        s.income.fields[Field.DEPRECIATION_AMORTIZATION] = 999.0   # 只是"管理费用里的折旧"
        s.income.rows.append(
            stm.StatementRow("管理费用：折旧", 999.0, Field.DEPRECIATION_AMORTIZATION, "t"))
        s.da = self._da(5000.0)                                    # 补充资料里的总额
        self.assertAlmostEqual(s.da_total(), 5000.0,
                              msg="附注总额被利润表的部分数盖掉了")

    def test_falls_back_to_statements_when_notes_empty(self):
        s = _set()                      # 利润表里有 50
        s.da = self._da(0.0)            # 没有补充资料（total 为 0/空）
        self.assertAlmostEqual(s.da_total(), 50.0, msg="附注空时该退回报表口径")

    def test_none_when_nothing_has_it(self):
        s = _set(drop=("da",))
        s.da = None
        self.assertIsNone(s.da_total())


@unittest.skipUnless((FITBIT / "R2.htm").exists(), "需要 Fitbit 10-K 材料")
class TestFitbitHasNoNotesRegression(unittest.TestCase):
    """Fitbit 没有「现金流量表补充资料」—— 必须照旧从报表里取到 D&A，不许被新优先级弄丢。"""

    def test_four_ratios_unchanged(self):
        cfg = {"statements": {
            "unit": "千美元", "as_of": "2016-12-31", "gaap": "US GAAP",
            "scope": "合并", "audited": "已审计",
            "balance_sheet": "materials/fitbit-2016-10k/R2.htm",
            "income_statement": "materials/fitbit-2016-10k/R4.htm",
            "cash_flow": "materials/fitbit-2016-10k/R8.htm"}}
        h = fc.load_from_config(cfg, ROOT).history()
        self.assertAlmostEqual(h["历史折旧摊销占收入比"], 0.0176, places=4)
        self.assertAlmostEqual(h["历史 EBITDA 率"], -0.0343, places=4)


class TestImplausibleMagnitudeIsFlagged(unittest.TestCase):
    """量级不对的取数必须自己喊出来 —— 这类错不报错、不让勾稽不平，最危险。

    实测：某 104 页扫描件（矿业）附注抽到的 D&A 不到收入的 0.1% ✗ —— 数字取到了，
    但显然错了（OCR 把密集数字读花，或单位口径不符）。报告如果照实印出"0.00%"，
    用户会以为公司真没折旧；印出"数据不足"又不对（数据是取到了的）。
    正确做法是**给数 + 明说这个数可疑**。
    """

    def test_too_small_da_is_flagged(self):
        h = _set(revenue=1000.0, da=0.5).history_notes()     # 0.05% → 不可能
        self.assertIn("历史折旧摊销占收入比", h)
        self.assertIn("常识", h["历史折旧摊销占收入比"])
        self.assertIn("历史 EBITDA 率", h, "EBITDA 率同样受影响，要一起提示")

    def test_too_large_da_is_flagged(self):
        h = _set(revenue=1000.0, da=900.0).history_notes()   # 90% → 不可能
        self.assertIn("历史折旧摊销占收入比", h)

    def test_normal_da_is_not_flagged(self):
        for da in (10.0, 50.0, 150.0):                       # 1% / 5% / 15% 都正常
            h = _set(revenue=1000.0, da=da).history_notes()
            self.assertNotIn("历史折旧摊销占收入比", h, f"正常量级被误报：{da}")


if __name__ == "__main__":
    unittest.main()
