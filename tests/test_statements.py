"""报表集接进 `value.py` 的测试。

## 三件必须锁住的事

**① 配置写错要说清楚是哪个键。** 三个路径键长得像，报错不指位置等于没报。

**② `apply_facts` 不能覆盖用户已经填的值。** 用户填了 net_debt 是明确表态，
   引擎算出来一个不同的数就把它盖掉，等于把用户的手动判断静默丢掉。

**③ 算不出来的不能假装算出来了。** 缺数据时要原样留着，
   让缺在引擎那里以「缺」的形式暴露，而不是被一个假数盖住。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import cli as fc  # noqa: E402
from financials import statements as stm  # noqa: E402
from financials.canonical import Field  # noqa: E402

FITBIT = ROOT / "materials" / "fitbit-2016-10k"


def _cfg(**over):
    cfg = {
        "statements": {
            "unit": "千美元",
            "as_of": "2016-12-31",
            "gaap": "US GAAP", "scope": "合并", "audited": "已审计",
            "balance_sheet": "materials/fitbit-2016-10k/R2.htm",
            "income_statement": "materials/fitbit-2016-10k/R4.htm",
            "cash_flow": "materials/fitbit-2016-10k/R8.htm",
        }
    }
    cfg["statements"].update(over)
    return cfg


@unittest.skipUnless((FITBIT / "R2.htm").exists(), "需要 Fitbit 10-K 材料")
class TestLoadFromConfig(unittest.TestCase):
    def test_loads_three_statements(self):
        s = fc.load_from_config(_cfg(), ROOT)
        self.assertIsNotNone(s.balance)
        self.assertIsNotNone(s.income)
        self.assertIsNotNone(s.cash_flow)

    def test_missing_file_names_the_key(self):
        """报错要指到具体是哪个键 —— 三个路径键长得像。"""
        with self.assertRaises(fc.StatementConfigError) as cm:
            fc.load_from_config(_cfg(balance_sheet="nope/R2.htm"), ROOT)
        self.assertIn("balance_sheet", str(cm.exception))

    def test_empty_section_raises(self):
        with self.assertRaises(fc.StatementConfigError):
            fc.load_from_config({"statements": {}}, ROOT)

    def test_warns_when_caliber_not_declared(self):
        """§4.1–4.4：准则/合并口径/审计状态没声明要提醒。"""
        cfg = _cfg()
        for k in ("gaap", "scope", "audited"):
            cfg["statements"].pop(k)
        s = fc.load_from_config(cfg, ROOT)
        self.assertEqual(len(s.warnings), 3)


@unittest.skipUnless((FITBIT / "R2.htm").exists(), "需要 Fitbit 10-K 材料")
class TestApplyFacts(unittest.TestCase):
    def _statements(self):
        return fc.load_from_config(_cfg(), ROOT)

    def test_fills_base_revenue(self):
        cfg = {"dcf": {}}
        filled = fc.apply_facts(cfg, self._statements())
        self.assertEqual(cfg["dcf"]["base_revenue"]["value"], 2169461.0)
        self.assertTrue(any("base_revenue" in f for f in filled))

    def test_fills_with_source_and_unit_not_bare_number(self):
        """**填裸数字会丢掉来源**，被标成「未注明（裸数字）」+ 低置信度。
        推算出来的东西有来源，必须带上 —— 追溯是这个产品的底线。

        单位也要带：报表是千美元，`value.py` 默认单位是万元。
        """
        cfg = {"dcf": {}}
        fc.apply_facts(cfg, self._statements())
        spec = cfg["dcf"]["base_revenue"]
        self.assertIsInstance(spec, dict)
        self.assertIn("三张表", spec["source"])
        self.assertEqual(spec["unit"], "千美元")
        self.assertEqual(spec["confidence"], "高", "勾稽全平时该给高置信度")

    def test_confidence_drops_when_checks_do_not_balance(self):
        """勾稽不平的时候推算结果本身就可疑 —— 至少有一行归属错了。"""
        s = self._statements()
        s.balance.fields[Field.TOTAL_ASSETS] = 1.0   # 故意弄不平
        cfg = {"dcf": {}}
        fc.apply_facts(cfg, s)
        self.assertEqual(cfg["dcf"]["base_revenue"]["confidence"], "中")

    def test_does_not_overwrite_user_value(self):
        """**用户填了就不能盖。** 那是他的判断，不是待补的空。"""
        cfg = {"dcf": {"base_revenue": {"value": 999.0, "source": "我的判断"}}}
        fc.apply_facts(cfg, self._statements())
        self.assertEqual(cfg["dcf"]["base_revenue"]["value"], 999.0)

    def test_fills_inferred_zero_debt(self):
        """零负债公司反推出来的 0 是**能算的**，应该填进去。"""
        cfg = {"dcf": {}}
        fc.apply_facts(cfg, self._statements())
        self.assertEqual(cfg["dcf"]["net_debt"]["value"], -706013.0)

    def test_leaves_uncomputable_alone(self):
        """算不出来的不能假装。"""
        s = stm.Statements(balance=stm.StatementSet(
            name="空表", source="x", fields={}))
        cfg = {"dcf": {}}
        fc.apply_facts(cfg, s)
        self.assertNotIn("net_debt", cfg["dcf"])

    def test_no_dcf_section_is_a_noop(self):
        self.assertEqual(fc.apply_facts({}, self._statements()), [])


@unittest.skipUnless((FITBIT / "R2.htm").exists(), "需要 Fitbit 10-K 材料")
class TestStatementsObject(unittest.TestCase):
    def test_three_checks_all_flat(self):
        s = fc.load_from_config(_cfg(), ROOT)
        checks = s.checks()
        self.assertEqual(len(checks), 3)
        for c in checks:
            self.assertTrue(c.ok, f"{c.name} 不平：{c.render()}")

    def test_history_reports_capex_as_positive(self):
        """资本开支在现金流量表里是**流出**（负数），
        但作为「占收入比」这个参考值应当是正数 ——
        给负数会让人以为资本开支为负，那是另一个意思。"""
        s = fc.load_from_config(_cfg(), ROOT)
        capex_ratio = s.history()["历史资本开支占收入比"]
        self.assertIsNotNone(capex_ratio)
        assert capex_ratio is not None
        self.assertGreater(capex_ratio, 0)

    def test_history_finds_da_in_cash_flow(self):
        """**D&A 通常在现金流量表的间接法里，不在利润表。**
        只看利润表会取不到，而它是 EBITDA 的关键组成。"""
        s = fc.load_from_config(_cfg(), ROOT)
        ratio = s.history()["历史折旧摊销占收入比"]
        self.assertIsNotNone(ratio, "没在现金流量表里找 D&A")
        assert ratio is not None
        self.assertAlmostEqual(ratio, 38133.0 / 2169461.0, places=5)


class TestMappingWarnings(unittest.TestCase):
    """映射率过低必须**显式警告** —— 这是"表没读懂"，不是"报表里没有"。

    实测：一份 176 页的保险公司中期报告 → 利润表 8/76、现金流量表 7/35，
    收入 / 营业利润 / 折旧摊销全没映射上。不说的时候，报告会**安静地**少掉
    估值结论，而人以为这份材料本来就缺数。
    """

    @staticmethod
    def _set(name: str, total: int, mapped: int,
             fields: dict | None = None) -> stm.StatementSet:
        rows = [stm.StatementRow(label=f"行{i}", value=1.0,
                                 field=(Field.REVENUE if i < mapped else None),
                                 via="")
                for i in range(total)]
        return stm.StatementSet(name=name, source="x.pdf", rows=rows,
                                fields=dict(fields or {}))

    def test_low_income_rate_warns(self):
        s = stm.Statements(balance=self._set("资产负债表", 33, 28),
                           income=self._set("利润表", 76, 8),
                           cash_flow=self._set("现金流量表", 35, 7))
        w = " ".join(s.mapping_warnings())
        self.assertIn("利润表只映射上 8/76 行", w)
        self.assertIn("没被读懂", w)
        # 现金流量表的比率**不单独报警** —— 明细行多不等于没读懂（见下一条）
        self.assertNotIn("现金流量表只映射", w)
        self.assertNotIn("资产负债表只映射", w)

    def test_cash_flow_rate_alone_does_not_warn(self):
        """回归：Fillbit 的形状 —— 现金流量表 24/61（39%）但关键科目都在。

        这是**误报现场**：一开始按 50% 一刀切，把这门健康的材料也打了警告。
        「会喊狼来了的检查比没有检查更坏」，所以比率判据只留给利润表。
        """
        income_fields = {Field.REVENUE: 1.0, Field.OPERATING_INCOME: 1.0,
                         Field.DEPRECIATION_AMORTIZATION: 1.0}
        cf_fields = {Field.CFO: 1.0}
        s = stm.Statements(balance=self._set("资产负债表", 35, 31),
                           income=self._set("利润表", 28, 16, income_fields),
                           cash_flow=self._set("现金流量表", 61, 24, cf_fields))
        self.assertEqual(s.mapping_warnings(), [])

    def test_missing_key_fields_are_named(self):
        """缺哪些科目要**点名** —— 不然人不知道该去补什么。"""
        s = stm.Statements(income=self._set("利润表", 76, 8))
        w = " ".join(s.mapping_warnings())
        self.assertIn("关键科目没映射上", w)
        self.assertIn("营业收入", w)
        self.assertIn("营业利润", w)
        self.assertIn("折旧与摊销", w)
        self.assertIn("经营活动产生的现金流量净额", w)
        self.assertIn("不可用", w)

    def test_healthy_mapping_does_not_warn(self):
        """映射正常时一个字都不该多说 —— 会喊狼来了的检查比没有检查更坏。"""
        income_fields = {Field.REVENUE: 1.0, Field.OPERATING_INCOME: 1.0,
                         Field.DEPRECIATION_AMORTIZATION: 1.0}
        cf_fields = {Field.CFO: 1.0}
        s = stm.Statements(balance=self._set("资产负债表", 33, 30),
                           income=self._set("利润表", 28, 20, income_fields),
                           cash_flow=self._set("现金流量表", 61, 40, cf_fields))
        self.assertEqual(s.mapping_warnings(), [])

    def test_report_shows_the_warning(self):
        """报告正文里必须有（命令行与网页走的是同一份正文）。"""
        s = stm.Statements(income=self._set("利润表", 76, 8))
        out: list[str] = []
        fc.render_statements(s, out)
        self.assertIn("这张表很可能没被读懂", "\n".join(out))


if __name__ == "__main__":
    unittest.main()
