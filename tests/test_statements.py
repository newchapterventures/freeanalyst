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


if __name__ == "__main__":
    unittest.main()
