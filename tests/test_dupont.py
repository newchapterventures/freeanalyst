"""杜邦分析的测试 —— 用最小的假对象，不碰真实材料。

钉住五件事：
  ① 正常公司：三因子相乘 = ROE（口径自洽的底线）
  ② 缺数：**留空 + 说明**，不拿 0 填
  ③ 金融企业：**不适用**，且要说出命中的信号
  ④ 非正数：不算（比率为负没有意义）
  ⑤ 占比不到阈值**不能**判成金融（会喊狼来了比不判更坏）

（结果里的比率都是 `float | None` —— 断言非 None 之后才当数用。
Pyright 认 `assert`，所以辅助函数里那一行不是装饰。）
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from financials.canonical import Field
from valuation import dupont


def _st(fields: dict) -> SimpleNamespace:
    """按**真实形状**造：`StatementSet.fields` 就是 `Field -> 数字`，没有包装对象。

    ★ 第一版这里造了带 `.value` 的包装对象 —— 于是 6 条测试**全过**，
      而真实材料上五份齐刷刷报"缺"：测试量的是我假想的结构，不是真实的结构。
      真实材料在 `tests/test_dupont_real_shape.py` 里另有对照。
    """
    return SimpleNamespace(fields=dict(fields))


def _S(bal=None, inc=None, unit="元", period="2024-12-31") -> SimpleNamespace:
    return SimpleNamespace(
        balance=_st(bal or {}), income=_st(inc or {}), cash_flow=None,
        unit=unit, period=period)


class TestDuPont(unittest.TestCase):
    @staticmethod
    def num(v: float | None) -> float:
        """把「逻辑上已经不是 None」的比率交给断言用（也让类型检查看得懂）。"""
        assert v is not None, "这一步本该有值"
        return float(v)

    def test_three_factor_is_self_consistent(self):
        S = _S(bal={Field.TOTAL_ASSETS: 1000.0, Field.EQUITY: 400.0},
               inc={Field.REVENUE: 500.0, Field.NET_INCOME: 40.0,
                    Field.PRETAX_INCOME: 50.0, Field.OPERATING_INCOME: 60.0})
        d = dupont.analyse(S)
        self.assertTrue(d.applicable)
        self.assertAlmostEqual(self.num(d.net_margin), 0.08)
        self.assertAlmostEqual(self.num(d.asset_turnover), 0.5)
        self.assertAlmostEqual(self.num(d.equity_multiplier), 2.5)
        self.assertAlmostEqual(self.num(d.roe), 0.08 * 0.5 * 2.5)
        self.assertAlmostEqual(self.num(d.roe), 0.1)
        self.assertAlmostEqual(self.num(d.roa), 0.04)
        self.assertAlmostEqual(self.num(d.tax_burden), 0.8)
        self.assertAlmostEqual(self.num(d.ebit_margin), 0.1)
        self.assertAlmostEqual(self.num(d.interest_burden), 50 / 60)

    def test_missing_numbers_are_left_blank_not_zero_filled(self):
        S = _S(bal={Field.TOTAL_ASSETS: 1000.0, Field.EQUITY: 400.0},
               inc={Field.REVENUE: 500.0})              # 净利缺
        d = dupont.analyse(S)
        self.assertFalse(d.applicable)
        self.assertIn("净利润", d.why_not)
        self.assertIsNone(d.roe)
        self.assertNotIn("净利润", d.inputs, "缺的项不该被塞进 inputs")

    def test_a_company_with_big_other_liabilities_is_NOT_declined(self):
        """★ 这条钉的是一个**教训**，不是功能。

        第一版我用「`其他非流动负债` 占总负债 ≥30%」当保险信号 ——
        结果国城矿业（矿业公司）被判成金融企业 ✗，而它的表里那一项确实占 51%。
        项目里 `financials/meta.py` 早就记过同一个坑（"某 377 页矿业公司…被判成金融"），
        以及那条结论：**误判的代价大于不判**。

        所以现在：**照常给数**，绝不因为某个"其他"科目占比大就说"不适用"。
        """
        S = _S(bal={Field.TOTAL_ASSETS: 1_000_000.0,
                    Field.TOTAL_LIABILITIES: 700_000.0,
                    Field.OTHER_NONCURRENT_LIABILITIES: 350_000.0,   # 占 50%
                    Field.EQUITY: 300_000.0},
               inc={Field.REVENUE: 200_000.0, Field.NET_INCOME: 15_000.0,
                    Field.PRETAX_INCOME: 20_000.0, Field.OPERATING_INCOME: 25_000.0})
        d = dupont.analyse(S)
        self.assertTrue(d.applicable, "占比大就说'不适用' = 误判，比不判更坏")
        self.assertIsNotNone(d.roe, "该给数就给数")
        self.assertEqual(d.signs, [], "不该再自动判金融企业")

    def test_small_non_financial_liability_does_not_trigger_the_branch(self):
        S = _S(bal={Field.TOTAL_ASSETS: 1_000.0, Field.TOTAL_LIABILITIES: 500.0,
                    Field.OTHER_NONCURRENT_LIABILITIES: 50.0,   # 只占 10%
                    Field.EQUITY: 500.0},
               inc={Field.REVENUE: 800.0, Field.NET_INCOME: 80.0,
                    Field.PRETAX_INCOME: 100.0, Field.OPERATING_INCOME: 120.0})
        d = dupont.analyse(S)
        self.assertTrue(d.applicable, "占比不够却被判成金融 = 误判，比漏判更坏")

    def test_nonpositive_inputs_are_not_used(self):
        S = _S(bal={Field.TOTAL_ASSETS: 1000.0, Field.EQUITY: -50.0},
               inc={Field.REVENUE: 500.0, Field.NET_INCOME: 40.0})
        d = dupont.analyse(S)
        self.assertFalse(d.applicable)
        self.assertIn("非正", d.why_not)

    def test_lines_are_human_readable(self):
        S = _S(bal={Field.TOTAL_ASSETS: 1000.0, Field.EQUITY: 400.0},
               inc={Field.REVENUE: 500.0, Field.NET_INCOME: 40.0,
                    Field.PRETAX_INCOME: 50.0, Field.OPERATING_INCOME: 60.0})
        txt = "\n".join(dupont.analyse(S).as_lines())
        self.assertIn("ROE", txt)
        self.assertIn("净利率", txt)
        self.assertIn("10.00%", txt)


if __name__ == "__main__":
    unittest.main()
