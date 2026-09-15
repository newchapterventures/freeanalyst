"""估值引擎的测试。

这个文件用**手算的已知答案**验证引擎。不是"跑通了"，是"算对了"。

估值算错不会报错，只会让你在对方面前报出一个错数字。所以每个用例的
期望值都是手工推导出来的，注释里留着推导过程。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valuation import (  # noqa: E402
    Assumption,
    Confidence,
    DcfInputs,
    MultiplesInputs,
    Purpose,
    Scenario,
    SdeBuild,
    Stage,
    Stance,
    WaccInputs,
    build_sde,
    comps_summary,
    compute_wacc,
    equity_bridge,
    enterprise_value,
    project,
    relever_beta,
    reverse_dcf_growth,
    run_dcf,
    run_multiples,
    sensitivity,
    unlever_beta,
)


def A(name, value, unit="万元", source="测试输入", conf=Confidence.HIGH):
    return Assumption(name, value, unit, source, conf)


SCENARIO = Scenario(
    purpose=Purpose.MNA, stance=Stance.BUYER, stage=Stage.MATURE,
    valuation_date="2026-06-30", currency="CNY",
)


def wacc_inputs(**over) -> WaccInputs:
    base = dict(
        risk_free=A("无风险利率", 0.020, "", "10年期国债", Confidence.HIGH),
        equity_risk_premium=A("股权风险溢价", 0.050, "", "市场基准", Confidence.MEDIUM),
        beta_unlevered=A("去杠杆beta", 1.0, "", "同业可比", Confidence.MEDIUM),
        tax_rate=A("所得税率", 0.25, "", "法定税率", Confidence.HIGH),
        cost_of_debt=A("债务成本", 0.050, "", "实际借款利率", Confidence.HIGH),
        debt=A("有息负债", 4000.0),
        equity=A("股权价值", 6000.0),
    )
    base.update(over)
    return WaccInputs(**base)


def dcf_inputs(**over) -> DcfInputs:
    base = dict(
        scenario=SCENARIO,
        years=[2026],
        base_revenue=A("基期收入", 900.0),
        revenue=[A("2026收入", 1000.0)],
        ebitda_margin=[A("2026 EBITDA率", 0.20, "")],
        tax_rate=A("所得税率", 0.25, "", "法定税率"),
        da_pct_revenue=A("折旧摊销占比", 0.05, ""),
        capex_pct_revenue=A("资本开支占比", 0.05, ""),
        nwc_pct_revenue=A("营运资本占比", 0.10, ""),
        terminal_growth=A("永续增长率", 0.03, ""),
        net_debt=A("净债务", 300.0, "万元", "2025审计报告"),
    )
    base.update(over)
    return DcfInputs(**base)


class TestWacc(unittest.TestCase):
    def test_hamada_relever(self):
        """β_L = β_U × [1 + (1−t)×D/E]
        = 1.0 × [1 + 0.75 × 4000/6000] = 1.0 × 1.5 = 1.5"""
        self.assertAlmostEqual(relever_beta(1.0, 4000, 6000, 0.25), 1.5, places=6)

    def test_unlever_is_inverse(self):
        """去杠杆应该能还原原值。"""
        b_l = relever_beta(1.0, 4000, 6000, 0.25)
        self.assertAlmostEqual(unlever_beta(b_l, 4000, 6000, 0.25), 1.0, places=6)

    def test_wacc_hand_computed(self):
        """
        β_L = 1.5
        Re  = 0.020 + 1.5 × 0.050 = 0.095
        E/V = 6000/10000 = 0.6, D/V = 0.4
        Rd×(1−t) = 0.050 × 0.75 = 0.0375
        WACC = 0.6 × 0.095 + 0.4 × 0.0375 = 0.057 + 0.015 = 0.072
        """
        r = compute_wacc(wacc_inputs())
        self.assertAlmostEqual(r.beta_levered, 1.5, places=6)
        self.assertAlmostEqual(r.cost_of_equity, 0.095, places=6)
        self.assertAlmostEqual(r.equity_weight, 0.6, places=6)
        self.assertAlmostEqual(r.after_tax_cost_of_debt, 0.0375, places=6)
        self.assertAlmostEqual(r.wacc, 0.072, places=6)

    def test_size_and_country_premium_flow_through(self):
        """加上规模溢价 2% 和国家风险 1%，Re 应该 +3%。"""
        r = compute_wacc(wacc_inputs(
            size_premium=A("规模溢价", 0.02, ""),
            country_risk_premium=A("国家风险溢价", 0.01, ""),
        ))
        self.assertAlmostEqual(r.cost_of_equity, 0.125, places=6)

    def test_missing_input_refuses_to_compute(self):
        """缺项必须报错，不许静默填默认值。"""
        bad = wacc_inputs(tax_rate=Assumption("所得税率", None, "", "未提供", Confidence.MISSING))
        with self.assertRaises(ValueError) as ctx:
            compute_wacc(bad)
        self.assertIn("所得税率", str(ctx.exception))


class TestDcf(unittest.TestCase):
    def test_projection_hand_computed(self):
        """
        EBITDA = 1000 × 0.20 = 200
        D&A    = 1000 × 0.05 = 50
        CapEx  = 1000 × 0.05 = 50
        NWC_t0 = 900 × 0.10 = 90   （基期收入，不是预测第一年）
        NWC_t1 = 1000 × 0.10 = 100
        ΔNWC   = 10

        FCFF = 200×(1−0.25) + 50×0.25 − 50 − 10
             = 150 + 12.5 − 50 − 10 = 102.5
        """
        rows, pv_sum, fcff_n = project(dcf_inputs(), 0.10)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].ebitda, 200.0, places=6)
        self.assertAlmostEqual(rows[0].fcff, 102.5, places=6)
        self.assertAlmostEqual(rows[0].pv, 102.5 / 1.10, places=6)
        self.assertAlmostEqual(pv_sum, 93.1818181818, places=6)
        self.assertAlmostEqual(fcff_n, 102.5, places=6)

    def test_nwc_uses_base_revenue_not_year_one(self):
        """
        如果营运资本起点错误地用了预测第一年的收入，ΔNWC 会变成 0，
        FCFF 会虚高 10（这是真实的建模错误，必须被这个测试挡住）。
        """
        rows, _pv, _f = project(dcf_inputs(), 0.10)
        self.assertAlmostEqual(rows[0].nwc, 100.0, places=6)
        # 若起点用错，FCFF 会是 112.5 而非 102.5
        self.assertNotAlmostEqual(rows[0].fcff, 112.5, places=6)

    def test_enterprise_and_equity_value(self):
        """
        TV    = 102.5 × 1.03 / (0.10 − 0.03) = 105.575 / 0.07 = 1508.2142857
        PV(TV)= 1508.2142857 / 1.10 = 1371.1038961
        EV    = 93.1818182 + 1371.1038961 = 1464.2857143
        Equity= 1464.2857143 − 300 = 1164.2857143
        """
        ins = dcf_inputs()
        ev, _trace, _rows = enterprise_value(ins, 0.10)
        self.assertAlmostEqual(ev, 1464.2857142857, places=4)
        eq = equity_bridge(ev, ins, enterprise_value(ins, 0.10)[1])
        self.assertAlmostEqual(eq, 1164.2857142857, places=4)

    def test_equity_bridge_subtracts_minority_and_adds_non_operating(self):
        """少数股东权益要扣，非经营性资产要加 —— 方向不能反。"""
        ins = dcf_inputs(
            net_debt=A("净债务", 300.0),
            minority_interest=A("少数股东权益", 100.0),
            non_operating_assets=A("非经营性资产", 50.0),
        )
        ev, trace, _ = enterprise_value(ins, 0.10)
        eq = equity_bridge(ev, ins, trace)
        self.assertAlmostEqual(eq, ev - 300 - 100 + 50, places=4)

    def test_terminal_value_share_warning(self):
        """终值占九成以上时必须提示 —— 这个估值主要由永续段撑着。"""
        r = run_dcf(dcf_inputs(), 0.10)
        joined = " ".join(r.notes)
        self.assertIn("终值占企业价值", joined)
        self.assertIn("75%", joined)  # 触发红旗

    def test_growth_above_wacc_is_rejected(self):
        """永续增长率 ≥ WACC 会让终值发散，必须拒绝计算。"""
        ins = dcf_inputs(terminal_growth=A("永续增长率", 0.12, ""))
        with self.assertRaises(ValueError) as ctx:
            enterprise_value(ins, 0.10)
        self.assertIn("发散", str(ctx.exception))

    def test_missing_net_debt_refuses_to_compute(self):
        ins = dcf_inputs(net_debt=Assumption("净债务", None, "", "未提供", Confidence.MISSING))
        with self.assertRaises(ValueError):
            run_dcf(ins, 0.10)

    def test_sensitivity_directions(self):
        """方向性：WACC 升 → 估值降；永续增长升 → 估值升。"""
        ins = dcf_inputs()
        waccs = [0.09, 0.10, 0.11]
        growths = [0.02, 0.03, 0.04]
        grid = sensitivity(ins, waccs, growths)

        # 同一 g 下，WACC 越高估值越低（第 0 列）
        col_g3 = [grid[i][1] for i in range(3)]   # g = 3%
        self.assertGreater(col_g3[0], col_g3[1])
        self.assertGreater(col_g3[1], col_g3[2])

        # 同一 WACC 下，增长率越高估值越高（中间行）
        row_w10 = grid[1]
        self.assertLess(row_w10[0], row_w10[1])
        self.assertLess(row_w10[1], row_w10[2])

    def test_sensitivity_marks_infeasible_cells(self):
        """g ≥ WACC 的格子填 nan，不能给出假数字。"""
        grid = sensitivity(dcf_inputs(), [0.03, 0.05], [0.08, 0.10])
        for row in grid:
            for v in row:
                self.assertTrue(v != v or v > 0, "不可行的格子必须是 nan")


class TestReverseDcf(unittest.TestCase):
    def test_recovers_known_growth(self):
        """正向算出 1164.2857（g=3%），反向必须解回 3%。"""
        ins = dcf_inputs()
        ev, trace, _ = enterprise_value(ins, 0.10)
        target = equity_bridge(ev, ins, trace)

        res = reverse_dcf_growth(ins, 0.10, target)
        self.assertTrue(res.feasible)
        self.assertAlmostEqual(res.solved_growth, 0.03, places=4)

    def test_reports_infeasible_when_price_too_high(self):
        """价格超出可行区间时，必须说清楚而不是硬给一个数。"""
        ins = dcf_inputs()
        res = reverse_dcf_growth(ins, 0.10, target_equity_value=99_999_999)
        self.assertFalse(res.feasible)
        self.assertIsNone(res.solved_growth)
        self.assertIn("超出可行区间", res.note)

    def test_higher_price_implies_higher_growth(self):
        """要价越高，隐含增长率越高 —— 单调性。"""
        ins = dcf_inputs()
        lo = reverse_dcf_growth(ins, 0.10, 1100).solved_growth
        hi = reverse_dcf_growth(ins, 0.10, 1400).solved_growth
        self.assertIsNotNone(lo)
        self.assertIsNotNone(hi)
        self.assertLess(lo, hi)


class TestMultiples(unittest.TestCase):
    def test_ebitda_multiple_hand_computed(self):
        """
        EBITDA = 3150，倍数 [8, 10, 12]
        EV     = 25200 / 31500 / 37800
        Equity = EV − 500 = 24700 / 31000 / 37300
        """
        r = run_multiples(MultiplesInputs(
            metric_name="EBITDA",
            metric_value=A("调整后EBITDA", 3150.0),
            multiple_low=A("倍数下沿", 8.0, "x"),
            multiple_mid=A("倍数中枢", 10.0, "x"),
            multiple_high=A("倍数上沿", 12.0, "x"),
            net_debt=A("净债务", 500.0),
        ))
        self.assertAlmostEqual(r.low, 24700.0, places=4)
        self.assertAlmostEqual(r.mid, 31000.0, places=4)
        self.assertAlmostEqual(r.high, 37300.0, places=4)

    def test_dlom_applied_after_bridge(self):
        """少数股权折价作用在股权价值上，不是企业价值上。"""
        r = run_multiples(MultiplesInputs(
            metric_name="EBITDA",
            metric_value=A("调整后EBITDA", 3150.0),
            multiple_low=A("倍数下沿", 8.0, "x"),
            multiple_mid=A("倍数中枢", 10.0, "x"),
            multiple_high=A("倍数上沿", 12.0, "x"),
            net_debt=A("净债务", 500.0),
            discount_for_lack_of_marketability=A("少数股权折价", 0.20, ""),
        ))
        self.assertAlmostEqual(r.low, 24700.0 * 0.8, places=4)
        self.assertAlmostEqual(r.mid, 31000.0 * 0.8, places=4)
        # 折扣必须被记录在注释里，且与"少数股东权益"区分开
        self.assertTrue(any("少数股东权益" in n for n in r.notes))

    def test_non_positive_metric_rejected(self):
        """负或零基数不能套乘数。"""
        with self.assertRaises(ValueError) as ctx:
            run_multiples(MultiplesInputs(
                metric_name="EBITDA",
                metric_value=A("调整后EBITDA", -100.0),
                multiple_low=A("倍数下沿", 8.0, "x"),
                multiple_mid=A("倍数中枢", 10.0, "x"),
                multiple_high=A("倍数上沿", 12.0, "x"),
            ))
        self.assertIn("不能作为乘数基数", str(ctx.exception))

    def test_non_monotonic_range_rejected(self):
        with self.assertRaises(ValueError):
            run_multiples(MultiplesInputs(
                metric_name="EBITDA",
                metric_value=A("EBITDA", 100.0),
                multiple_low=A("倍数下沿", 12.0, "x"),
                multiple_mid=A("倍数中枢", 10.0, "x"),
                multiple_high=A("倍数上沿", 8.0, "x"),
            ))


class TestSde(unittest.TestCase):
    def test_sde_hand_computed(self):
        """
        EBITDA = 2000 + 100 + 300 + 250 = 2650
        薪酬调整 = 500 − 200 = 300
        SDE    = 2650 + 300 + 50 = 3000
        """
        sde, _trace, _ins = build_sde(SdeBuild(
            net_income=A("净利润", 2000.0),
            interest=A("利息支出", 100.0),
            taxes=A("所得税", 300.0),
            depreciation_amortization=A("折旧摊销", 250.0),
            owner_compensation_actual=A("实控人实发薪酬", 500.0),
            owner_compensation_market=A("市场水平薪酬", 200.0),
            personal_expenses=A("个人性质费用", 50.0),
        ))
        self.assertAlmostEqual(sde.value, 3000.0, places=4)

    def test_one_time_gains_subtracted_losses_added(self):
        """一次性收益要减掉，一次性损失要加回 —— 方向不能反。"""
        sde, _t, _i = build_sde(SdeBuild(
            net_income=A("净利润", 2000.0),
            interest=A("利息支出", 0.0),
            taxes=A("所得税", 0.0),
            depreciation_amortization=A("折旧摊销", 0.0),
            owner_compensation_actual=A("实发薪酬", 0.0),
            owner_compensation_market=A("市场薪酬", 0.0),
            one_time_gains=A("一次性收益", 400.0),
            one_time_losses=A("一次性损失", 100.0),
        ))
        self.assertAlmostEqual(sde.value, 2000.0 - 400.0 + 100.0, places=4)


class TestComps(unittest.TestCase):
    def test_quantiles_hand_computed(self):
        """[8, 9, 10, 11, 12] → P25=9 中位10 P75=11 均值10"""
        peers = [
            {"name": f"可比{i}", "ev_ebitda": v, "source": "SEC EDGAR 2025-12-31"}
            for i, v in enumerate([10, 12, 8, 11, 9])  # 故意乱序
        ]
        stats, _trace, assumptions = comps_summary(peers, "ebitda", "ev_ebitda")
        self.assertEqual(stats["n"], 5)
        self.assertAlmostEqual(stats["min"], 8.0, places=6)
        self.assertAlmostEqual(stats["p25"], 9.0, places=6)
        self.assertAlmostEqual(stats["median"], 10.0, places=6)
        self.assertAlmostEqual(stats["p75"], 11.0, places=6)
        self.assertAlmostEqual(stats["max"], 12.0, places=6)
        self.assertAlmostEqual(stats["mean"], 10.0, places=6)
        self.assertEqual(len(assumptions), 5)

    def test_missing_source_marked_low_confidence(self):
        """没有来源的倍数必须标低置信度。"""
        peers = [{"name": "A", "ev_ebitda": 8.0}, {"name": "B", "ev_ebitda": 9.0},
                 {"name": "C", "ev_ebitda": 10.0}]
        _stats, _trace, assumptions = comps_summary(peers, "ebitda", "ev_ebitda")
        self.assertTrue(all(a.confidence is Confidence.LOW for a in assumptions))

    def test_sample_too_small(self):
        """少于 3 家不出区间。"""
        stats, _trace, _a = comps_summary(
            [{"name": "A", "ev_ebitda": 8.0}, {"name": "B", "ev_ebitda": 9.0}],
            "ebitda", "ev_ebitda")
        self.assertEqual(stats, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
