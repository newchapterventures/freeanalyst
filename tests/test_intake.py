"""intake 的测试 —— 重点是**它会不会替用户猜**。

这个模块的每一处判断都有一条共同纪律：**认不出来就说认不出来。**
所以用例集中在「认不出来时它有没有停下来」和「它有没有偷偷填一个数」：
单位、表归属、年数、缺口 —— 这四处只要有一处开始猜，产出的报告就不可追溯。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import intake  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FITBIT = REPO / "materials" / "fitbit-2016-10k"


class TestUnitDetection(unittest.TestCase):
    """单位猜错是 1000 倍级的**静默**错误，所以这层宁可认不出。"""

    def test_chinese_declaration(self):
        self.assertEqual(intake.detect_unit("编制单位：某公司　单位：万元")[0], "万元")

    def test_chinese_word(self):
        self.assertEqual(intake.detect_unit("人民币千元")[0], "千元")

    def test_english_in_thousands(self):
        self.assertEqual(intake.detect_unit("(In thousands, except share data)")[0],
                         "千美元")

    def test_english_in_millions(self):
        self.assertEqual(intake.detect_unit("(In millions)")[0], "百万美元")

    def test_nothing_found_returns_empty(self):
        """**认不出必须返回空** —— 返回空才会停下来问用户。"""
        self.assertEqual(intake.detect_unit("这是一段没有任何单位提示的话")[0], "")

    def test_empty_text(self):
        self.assertEqual(intake.detect_unit("")[0], "")


class TestClassify(unittest.TestCase):
    """英文材料的中文标志全失效，所以要有一组英文标志兜住。"""

    def test_english_balance_sheet(self):
        labels = (" ".join(["Cash and cash equivalents", "Total current assets",
                            "Total assets", "Total current liabilities",
                            "Total liabilities and stockholders' equity"]))
        self.assertEqual(intake.classify(labels, 20), "balance")

    def test_english_income_statement(self):
        labels = " ".join(["Revenue", "Cost of revenue", "Gross profit",
                           "Operating income", "Net income"])
        self.assertEqual(intake.classify(labels, 15), "income")

    def test_english_cash_flow(self):
        labels = " ".join(["Cash flows from operating activities",
                           "Cash flows from investing activities",
                           "Cash flows from financing activities",
                           "Cash and cash equivalents at end of period"])
        self.assertEqual(intake.classify(labels, 30), "cash_flow")

    def test_unrecognized_is_unknown(self):
        self.assertEqual(intake.classify("Notes to financial statements", 12),
                         "unknown")


class TestAnswers(unittest.TestCase):
    """问答清单是人和机器之间唯一的接口，读错一处就会替人做判断。"""

    def _read(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text(text, encoding="utf-8")
            return intake.read_answers(p)

    def test_plain_value_has_no_source(self):
        a = self._read("risk_free = 0.024")["risk_free"]
        self.assertEqual(a.value, "0.024")
        self.assertEqual(a.source, "")
        self.assertEqual(a.confidence, "低")      # 没来源就是低置信度

    def test_source_makes_it_medium(self):
        a = self._read("risk_free = 0.024  @美国 10 年期国债")["risk_free"]
        self.assertEqual(a.source, "美国 10 年期国债")
        self.assertEqual(a.confidence, "中")

    def test_explicit_confidence_prefix(self):
        a = self._read("tax_rate = 0.35 @高:法定税率")["tax_rate"]
        self.assertEqual(a.confidence, "高")
        self.assertEqual(a.source, "法定税率")

    def test_comment_and_blank_lines_ignored(self):
        got = self._read("# 注释\n\n[场景]\n\n\npurpose = 并购定价\n")
        self.assertEqual(list(got), ["purpose"])

    def test_trailing_comment_stripped(self):
        got = self._read("target = 某公司　# 标的名称\n")
        self.assertEqual(got["target"].value, "某公司")

    def test_punctuation_only_value_is_empty(self):
        """模板里渲染成一串逗号的「空」，不能被读成一个值。"""
        self.assertEqual(self._read("growth = ，，，，\n"), {})

    def test_percent_parsed_as_fraction(self):
        got = self._read("risk_free = 2.4%\n")
        self.assertAlmostEqual(intake._num(got["risk_free"]), 0.024)


class TestTemplate(unittest.TestCase):
    """模板必须把「什么必须你给」说清楚，而且**不能替人填假设**。"""

    @classmethod
    def setUpClass(cls):
        cls.mat = intake.scan(FITBIT)

    def test_every_question_has_a_line(self):
        tpl = intake.render_template(intake.questions(self.mat), self.mat)
        for q in intake.questions(self.mat):
            self.assertIn(f"{q.key} ", tpl, q.key)

    def test_judgement_keys_are_left_blank(self):
        """增长率、EBITDA 率这些是判断，**必须留空**。"""
        tpl = intake.render_template(intake.questions(self.mat), self.mat)
        for key in ("growth", "ebitda_margin", "terminal_growth",
                    "multiple_low"):
            line = next(x for x in tpl.splitlines() if x.startswith(f"{key} "))
            self.assertEqual(line.split("=")[1].split("#")[0].strip("　 "), "",
                             f"{key} 被填了默认值")

    def test_material_facts_appear_as_reference_not_as_value(self):
        """历史参考只能出现在注释里 —— 出现在等号右边就是替人做了判断。"""
        tpl = intake.render_template(intake.questions(self.mat), self.mat)
        line = next(x for x in tpl.splitlines() if x.startswith("da_pct_revenue"))
        self.assertEqual(line.split("=")[1].split("#")[0].strip("　 "), "")
        self.assertIn("参考", line)

    def test_detected_unit_shown(self):
        tpl = intake.render_template(intake.questions(self.mat), self.mat)
        self.assertIn("千美元", tpl)


class TestScanFitbit(unittest.TestCase):
    """拿真实材料跑：10-K 的三张表要认得出来，认不出的要列出来。"""

    @classmethod
    def setUpClass(cls):
        cls.mat = intake.scan(FITBIT)

    def test_three_statements_detected(self):
        self.assertEqual(self.mat.detected.get("balance"), "R2.htm")
        self.assertEqual(self.mat.detected.get("income"), "R4.htm")
        self.assertEqual(self.mat.detected.get("cash_flow"), "R8.htm")

    def test_unit_detected(self):
        self.assertEqual(self.mat.unit, "千美元")

    def test_scope_is_consolidated_not_standalone(self):
        """**标题也要读进来判**：只看行标签会把合并表判成单体。"""
        S = self.mat.statements
        assert S is not None
        self.assertEqual(S.scope, "合并")

    def test_unused_files_are_listed_not_silently_skipped(self):
        self.assertTrue(any("R5.htm" in u for u in self.mat.unused))

    def test_arbitration_checks_run(self):
        S = self.mat.statements
        assert S is not None
        checks = S.checks()
        self.assertTrue(any(c.ok for c in checks))


class TestBuildConfig(unittest.TestCase):
    """配置的形状要和 `value.py` 对得上 —— 尤其 `years` 是**年份列表**。"""

    @classmethod
    def setUpClass(cls):
        cls.mat = intake.scan(FITBIT)

    def _answers(self, **over):
        base = {
            "unit": "千美元",
            "risk_free": "0.0245 @高:国债",
            "equity_risk_premium": "0.055 @高:ERP",
            "beta_unlevered": "1.10 @演示",
            "cost_of_debt": "0.045 @演示",
            "tax_rate": "0.35 @高:法定",
            "debt": "0 @高:无有息负债",
            "equity": "800000 @演示",
            "growth": "0.05, 0.05, 0.04 @演示",
            "ebitda_margin": "0.06, 0.07, 0.08 @演示",
            "da_pct_revenue": "0.0176 @中:历史",
            "capex_pct_revenue": "0.0362 @中:历史",
            "nwc_pct_revenue": "0.1818 @中:历史",
            "terminal_growth": "0.025 @中",
            "valuation_date": "2016-12-31",
        }
        base.update(over)
        return {k: intake.parse_answer(v) for k, v in base.items()}

    def test_years_is_a_year_label_list(self):
        """回归用例：`years` 曾经被写成 `5`，dcf.py 拿它 len() 直接崩。"""
        cfg, _ = intake.build_config(self.mat, self._answers())
        self.assertIsInstance(cfg["dcf"]["years"], list)
        self.assertEqual(cfg["dcf"]["years"], [2017, 2018, 2019])

    def test_revenue_derived_from_base_and_growth(self):
        cfg, _ = intake.build_config(self.mat, self._answers())
        base = float(cfg["dcf"]["base_revenue"]["value"])
        revs = [x["value"] for x in cfg["dcf"]["revenue"]]
        self.assertAlmostEqual(revs[0], base * 1.05, places=4)
        self.assertEqual(len(revs), 3)

    def test_single_margin_expands_to_all_years(self):
        cfg, _ = intake.build_config(self.mat, self._answers(ebitda_margin="0.06 @演示"))
        self.assertEqual(len(cfg["dcf"]["ebitda_margin"]), 3)

    def test_missing_assumptions_are_reported_not_filled(self):
        cfg, missing = intake.build_config(self.mat, self._answers(risk_free=""))
        self.assertNotIn("wacc", cfg)          # 缺一项就整块不跑
        self.assertTrue(any("risk_free" in m for m in missing))

    def test_missing_growth_means_no_dcf_section(self):
        cfg, missing = intake.build_config(self.mat, self._answers(growth=""))
        self.assertNotIn("dcf", cfg)
        self.assertTrue(any("growth" in m for m in missing))

    def test_unit_is_carried_into_assumptions(self):
        cfg, _ = intake.build_config(self.mat, self._answers())
        self.assertEqual(cfg["dcf"]["revenue"][0]["unit"], "千美元")

    def test_sources_are_carried_into_config(self):
        """来源必须进配置 —— 追踪是底线，不能只活在问答清单里。"""
        cfg, _ = intake.build_config(self.mat, self._answers())
        self.assertIn("国债", cfg["wacc"]["risk_free"]["source"])

    def test_materials_section_records_provenance(self):
        cfg, _ = intake.build_config(self.mat, self._answers())
        self.assertEqual(cfg["materials"]["detected"]["balance"], "R2.htm")
        self.assertEqual(cfg["materials"]["unit"], "千美元")


class TestAppraiseFlow(unittest.TestCase):
    """端到端的形态：没给答案时出清单，单位没定时**停下来**。"""

    def test_template_written_when_no_answers(self):
        with tempfile.TemporaryDirectory() as d:
            dummy = Path(d) / "materials"
            dummy.mkdir()
            (dummy / "a.htm").write_text(
                "<table>"
                "<tr><td>货币资金</td><td>100</td></tr>"
                "<tr><td>应收账款</td><td>200</td></tr>"
                "<tr><td>存货</td><td>50</td></tr>"
                "<tr><td>流动资产合计</td><td>350</td></tr>"
                "<tr><td>资产总计</td><td>1000</td></tr>"
                "<tr><td>短期借款</td><td>0</td></tr>"
                "<tr><td>流动负债合计</td><td>200</td></tr>"
                "<tr><td>负债合计</td><td>400</td></tr>"
                "<tr><td>所有者权益合计</td><td>600</td></tr>"
                "<tr><td>负债和所有者权益总计</td><td>1000</td></tr>"
                "</table>", encoding="utf-8")
            rc = intake.appraise(dummy, out_dir=Path(d) / "out")
            self.assertEqual(rc, 0)
            self.assertTrue((Path(d) / "out" / "估值问答.txt").exists())
            # 单位认不出来时必须**写在清单里**让人看见
            self.assertIn("必须你声明",
                          (Path(d) / "out" / "估值问答.txt").read_text(encoding="utf-8"))

    def test_stops_when_unit_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            dummy = Path(d) / "materials"
            dummy.mkdir()
            # 有表、但**任何单位提示都没有** —— 这时候不能算
            (dummy / "a.htm").write_text(
                "<table><tr><td>货币资金</td><td>1</td></tr>"
                "<tr><td>资产总计</td><td>1</td></tr>"
                "<tr><td>负债合计</td><td>0</td></tr>"
                "<tr><td>所有者权益合计</td><td>1</td></tr></table>",
                encoding="utf-8")
            ans = Path(d) / "ans.txt"
            ans.write_text("growth = 0.05\n", encoding="utf-8")
            rc = intake.appraise(dummy, ans, out_dir=Path(d) / "out")
            self.assertEqual(rc, 1)            # 单位没确定 → 不往下算

    def test_config_written_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            ans = Path(d) / "ans.txt"
            ans.write_text("unit = 千美元\n", encoding="utf-8")
            intake.appraise(FITBIT, ans, out_dir=out)
            files = list(out.glob("*.config.json"))
            self.assertEqual(len(files), 1)
            cfg = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertIn("materials", cfg)


class TestSingleFile(unittest.TestCase):
    """**真实材料常常就是一份 PDF，不是一个整理好的目录。**

    拿真实标的跑第一遍时踩到的第一个坑：只吃目录的话，第一句话就把人挡在门外。
    """

    def test_scan_accepts_a_single_file(self):
        mat = intake.scan(FITBIT / "R2.htm")
        self.assertTrue(mat.is_file)
        self.assertEqual(mat.detected.get("balance"), "R2.htm")

    def test_label_drops_the_suffix(self):
        mat = intake.scan(FITBIT / "R2.htm")
        self.assertEqual(mat.label, "R2")

    def test_template_lands_in_out_not_next_to_the_material(self):
        """**工具不该往用户的材料库里写东西** —— 材料目录是只读输入。

        第一版把问答清单写在材料旁边，实测往三个真实项目的私有目录里
        各扔了一份。现在统一落到 `out/<标的>/`。
        """
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "某标的年报.htm"
            src.write_text(
                "<table>"
                "<tr><td>货币资金</td><td>100</td></tr>"
                "<tr><td>应收账款</td><td>200</td></tr>"
                "<tr><td>存货</td><td>50</td></tr>"
                "<tr><td>资产总计</td><td>1000</td></tr>"
                "<tr><td>负债合计</td><td>400</td></tr>"
                "<tr><td>所有者权益合计</td><td>600</td></tr>"
                "</table>", encoding="utf-8")
            out = Path(d) / "结果"
            self.assertEqual(intake.appraise(src, out_dir=out), 0)
            self.assertTrue((out / "估值问答.txt").exists())
            # 材料目录里不该多出任何东西
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()),
                             ["某标的年报.htm", "结果"])

    def test_missing_path_says_so(self):
        with self.assertRaises(FileNotFoundError):
            intake.scan("/tmp/根本没有这个文件.pdf")


class TestChoiceFields(unittest.TestCase):
    """**能确定的就不要人打字。**

    界面上做成下拉的字段（立场、合并/单体、审计状态……）靠的就是这里的 options；
    建议值（单位、货币）靠 suggest。这条链断了，界面上就退回成让人手打。
    """

    @classmethod
    def setUpClass(cls):
        cls.q = {q.key: q for q in intake.questions(intake.scan(str(FITBIT)))}

    def test_stance_is_a_closed_choice(self):
        """立场：买方 / 卖方 / 中立 —— 必须是选项，不是手打。"""
        self.assertEqual(self.q["stance"].options, ("买方", "卖方", "中立"))
        self.assertIn(self.q["stance"].default, self.q["stance"].options)

    def test_other_closed_choices_are_offered(self):
        for key in ("purpose", "stage", "metric_name", "advisor_market",
                    "audited", "scope", "gaap"):
            self.assertTrue(self.q[key].options, f"{key} 应该给成下拉选项")

    def test_advisor_metric_options_match_the_engine(self):
        """下拉里给的指标必须是引擎真支持的 —— 否则用户选了也白选。"""
        from valuation.advisor import METRIC_FUNCS
        self.assertEqual(set(self.q["advisor_metric"].options), set(METRIC_FUNCS))

    def test_default_is_always_selectable(self):
        """默认值必须在选项里（或为空）—— 否则下拉会显示成空白，看着像数据丢了。"""
        for q in self.q.values():
            if q.options:
                self.assertTrue(q.default == "" or q.default in q.options,
                                f"{q.key}: 默认值 {q.default!r} 不在选项里")

    def test_detected_value_is_included_first(self):
        """材料推出来的口径要在选项里（放第一个）—— 认出来一个不常见写法也能选得中。"""
        scope = self.q["scope"]
        self.assertTrue(scope.options)
        if scope.default:
            self.assertEqual(scope.options[0], scope.default)

    def test_open_but_limited_fields_use_suggest(self):
        """单位/货币"有限但不封闭" → 可编辑下拉：能点选，也能写清单外的。"""
        for key in ("unit", "currency"):
            self.assertTrue(self.q[key].suggest, f"{key} 应该给建议值")
            self.assertFalse(self.q[key].options, f"{key} 不该做成封闭下拉")
        self.assertIn("千美元", self.q["unit"].suggest)

    def test_template_shows_the_choices(self):
        """txt 清单里也要把可选值写出来 —— 命令行那条路同样不该靠猜。"""
        txt = intake.render_template(list(self.q.values()), intake.scan(str(FITBIT)))
        self.assertIn("可选：买方 / 卖方 / 中立", txt)


class TestPercentFields(unittest.TestCase):
    """百分比字段：**界面上把 % 显示在框里，人只填数值**（填 8.5 就是 8.5%）。

    为什么非做不可：引擎只认"带单位的字符串"。人填一个光秃秃的 `8.5`，
    会被读成 850% —— 100 倍级的**静默**错误，报告照样出得来。
    """

    @classmethod
    def setUpClass(cls):
        cls.mat = intake.scan(str(FITBIT))
        cls.q = {q.key: q for q in intake.questions(cls.mat)}

    def test_percent_fields_are_marked(self):
        expect = {"equity_scope", "risk_free", "equity_risk_premium", "cost_of_debt",
                  "tax_rate", "growth", "ebitda_margin", "da_pct_revenue",
                  "capex_pct_revenue", "nwc_pct_revenue", "terminal_growth",
                  "sens_wacc", "sens_growth"}
        got = {k for k, q in self.q.items() if q.unit == "%"}
        self.assertEqual(got, expect, "百分比字段必须逐个标 unit='%'")

    def test_plain_numbers_are_not_marked_as_percent(self):
        """beta、倍数、金额都是"就这么个数" —— 标成百分比会让人把它当百分数填。"""
        for k in ("beta_unlevered", "debt", "equity", "multiple_low", "multiple_mid",
                  "multiple_high", "exit_multiple", "ask_price"):
            self.assertEqual(self.q[k].unit, "", k)

    def test_template_says_how_to_fill_percent(self):
        """txt 清单那条路要写**它自己的**规矩：写 `8.5%` 或 `0.085`。

        网页向导上那句"只填数值（8.5 即 8.5%）"**不能**照搬到清单里 ——
        清单没有补 % 的规则，照着写 `8.5` 会变成 850%。两个入口各说各的。
        """
        txt = intake.render_template(list(self.q.values()), self.mat)
        self.assertIn("按百分数填：写 8.5% 或 0.085", txt)
        self.assertNotIn("只写数值", txt)


class TestDerivedEbitda(unittest.TestCase):
    """EBITDA 必须从**三张表**里取数 —— 折旧摊销常在现金流量表的间接法段，不在利润表。

    实测踩到过：intake 只把利润表喂给 `derive.ebitda`，于是 Fitbit 这类材料的
    EBITDA 直接是 None（乘数法跟着整块空掉），而同一个仓库里 `history()` 与
    `datasources` 两条路都取到了 —— 知识在，只是没共用同一个取数口径。
    """

    @classmethod
    def setUpClass(cls):
        cls.mat = intake.scan(str(FITBIT))
        cls.st = cls.mat.statements

    def test_da_total_is_public(self):
        """取数口径只留一个公开入口，别的模块不该自己去翻表。"""
        st = self.st
        assert st is not None
        self.assertIsNotNone(st.da_total())

    def test_ebitda_is_derived_from_all_statements(self):
        from financials.canonical import Field
        st = self.st
        assert st is not None and st.income is not None
        e = intake._facts_number(self.mat, "ebitda")
        self.assertIsNotNone(e, "折旧摊销在现金流量表里 —— 只看利润表就取不到")
        assert e is not None
        oi = st.income.fields[Field.OPERATING_INCOME]
        da = st.da_total()
        assert da is not None
        self.assertAlmostEqual(e, oi + da)

    def test_matches_the_history_ratio(self):
        """推导出的 EBITDA 与引擎自己那栏「历史 EBITDA 率」必须对得上。

        两处算出来不一样，就说明取数口径又不一致了 —— 这正是上一次的病根。
        """
        from financials.canonical import Field
        st = self.st
        assert st is not None and st.income is not None
        e = intake._facts_number(self.mat, "ebitda")
        h = st.history()["历史 EBITDA 率"]
        assert e is not None and h is not None
        rev = st.income.fields[Field.REVENUE]
        self.assertAlmostEqual(e / rev, h, places=6)


class TestForecastReferences(unittest.TestCase):
    """预测参数与乘数指标都必须带**本期报告数据**做参考。

    自己预测的人得先看见"现在是多少"。参考值只作参照、**不代填** ——
    这是产品的界线：事实自动填，假设绝不自动填。
    """

    @classmethod
    def setUpClass(cls):
        cls.mat = intake.scan(str(FITBIT))
        cls.q = {q.key: q for q in intake.questions(cls.mat)}
        assert cls.mat.statements is not None
        cls.hist = cls.mat.statements.history()

    def test_every_forecast_field_has_a_reference(self):
        for k in ("growth", "ebitda_margin", "da_pct_revenue",
                  "capex_pct_revenue", "nwc_pct_revenue", "terminal_growth"):
            self.assertTrue(self.q[k].reference, f"{k} 没有本期参考")

    def test_reference_never_prefills_the_input(self):
        """参考只出现在参考栏 —— 输入框必须还是空的（参考不许变成默认值）。"""
        for k in ("growth", "ebitda_margin", "da_pct_revenue",
                  "capex_pct_revenue", "nwc_pct_revenue"):
            self.assertEqual(self.q[k].default, "", f"{k} 被预填了")

    def test_historical_ratios_match_the_engine(self):
        """四项比率的参考 = 引擎自己算的历史值（同一个数，不许两处各算一遍）。"""
        for key, hkey in (("ebitda_margin", "历史 EBITDA 率"),
                          ("da_pct_revenue", "历史折旧摊销占收入比"),
                          ("capex_pct_revenue", "历史资本开支占收入比"),
                          ("nwc_pct_revenue", "历史净营运资本占收入比")):
            v = self.hist.get(hkey)
            self.assertIsNotNone(v, hkey)
            self.assertIn(f"{v:.2%}", self.q[key].reference)

    def test_growth_reference_says_why_it_cannot_be_derived(self):
        """增长率推不出来就**明说**（材料只有一期），别拿"行业增速"之类的顶上。"""
        ref = self.q["growth"].reference
        self.assertIn("材料只有一期", ref)
        self.assertIn("2,169,461", ref)              # 本期收入作为规模参照
        self.assertIn("千美元", ref)                  # 单位要带上（错单位=1000 倍错）

    def test_metric_reference_lists_every_option(self):
        """乘数指标那一栏：四个选项一个都不能少，能算的给数、算不了的给原因。"""
        ref = self.q["metric_name"].reference
        for o in self.q["metric_name"].options:
            self.assertIn(o, ref)
        self.assertIn("-74,332", ref)                # EBITDA = 营业利润 + 折旧摊销
        self.assertIn("-112,465", ref)               # EBIT = 营业利润
        self.assertIn("2,169,461", ref)              # 收入
        self.assertIn("需所有者薪酬", ref)            # SDE 为什么推不出

    def test_every_metric_option_is_wired_in_the_engine(self):
        """下拉里给的每个指标，选了之后引擎**真的能算** —— 否则就是骗人。

        SDE 是唯一例外，而它必须**明说推不出**（不静默、不给一个看起来对的数）。
        """
        base = {"unit": intake.parse_answer("千美元"),
                "multiple_low": intake.parse_answer("8"),
                "multiple_mid": intake.parse_answer("10"),
                "multiple_high": intake.parse_answer("12")}
        for metric, works in (("EBITDA", True), ("EBIT", True),
                              ("收入", True), ("SDE", False)):
            ans = dict(base, metric_name=intake.parse_answer(metric))
            cfg, missing = intake.build_config(self.mat, ans)
            if works:
                self.assertIn("multiples", cfg, metric)
                self.assertEqual(cfg["multiples"]["metric_name"], metric)
                self.assertIsNotNone(cfg["multiples"]["metric_value"]["value"])
            else:
                self.assertNotIn("multiples", cfg)
                self.assertTrue(any("需所有者薪酬" in m for m in missing), missing)


class TestOrigins(unittest.TestCase):
    """每个输入都要回答「这个数从哪来」：财报 / 财报推算 / 外部 / 判断。

    这条界线是这个工具的核心承诺：
      * **财报里有的**（EBIT、EBITDA、有息负债…）→ 工具**必须给出来**
      * **按财报能推、口径要人定**（有效税率、债务成本）→ 给数 + 请确认；
        没有数据就明说没有
      * **公司之外的市场信息**（无风险利率、ERP、beta、市值）→ 由用户提供，
        工具不编 —— 尽调材料里不可能包含公司之外的所有信息
    """

    @classmethod
    def setUpClass(cls):
        cls.mat = intake.scan(str(FITBIT))
        cls.q = {q.key: q for q in intake.questions(cls.mat)}

    def test_every_field_declares_its_origin(self):
        for q in self.q.values():
            self.assertIn(q.origin, intake.ORIGINS, f"{q.key} 没标来源类别")

    def test_external_fields_say_the_user_provides_them(self):
        """外部数据必须写清"由你提供" —— 不能让人以为工具会给。"""
        for k in ("risk_free", "equity_risk_premium", "beta_unlevered", "equity"):
            self.assertEqual(self.q[k].origin, intake.ORIGIN_EXTERNAL, k)
            self.assertIn("你提供", self.q[k].reference, k)

    def test_filings_fields_are_given_not_left_to_the_user(self):
        """财报里有的数，工具必须给出来（不是让人自己去翻表）。"""
        self.assertEqual(self.q["debt"].origin, intake.ORIGIN_FILING)
        self.assertTrue(self.q["debt"].reference)
        self.assertIn("材料里没有", self.q["debt"].reference)   # Fitbit 本期无借款

    def test_tax_rate_refuses_a_meaningless_ratio(self):
        """亏损年度不给有效税率 —— 两个负数相除出来的比率没有意义。"""
        q = self.q["tax_rate"]
        self.assertEqual(q.origin, intake.ORIGIN_EXTERNAL)
        self.assertIn("利润总额为负", q.reference)
        self.assertIn("请按法定税率填", q.reference)

    def test_template_prints_the_origin(self):
        txt = intake.render_template(list(self.q.values()), self.mat)
        self.assertIn("【财报】", txt)
        self.assertIn("【外部】", txt)


class TestTaxAndDebtCostDerivation(unittest.TestCase):
    """「材料里可能有」的第三种情况：**有数据就给数，并标明请确认**。

    真材料只落进其中一种情况，所以这里用假 statements 把三条分支都打一遍。
    """

    @staticmethod
    def _mat(inc: dict | None = None, bal: dict | None = None):
        from types import SimpleNamespace
        from typing import cast
        st = SimpleNamespace(income=SimpleNamespace(fields=inc or {}),
                             balance=SimpleNamespace(fields=bal or {}),
                             cash_flow=None)
        # 只为打分支：这几个 helper 只用 statements.income / .balance 两处
        return cast(intake.Materials, SimpleNamespace(statements=st))

    def test_effective_tax_rate_when_available(self):
        from financials.canonical import Field
        mat = self._mat({Field.INCOME_TAX: 250.0, Field.PRETAX_INCOME: 1000.0})
        ref, origin = intake._tax_reference(mat)
        self.assertEqual(origin, intake.ORIGIN_DERIVED)
        self.assertIn("25.00%", ref)
        self.assertIn("请确认", ref)

    def test_effective_tax_rate_refused_when_pretax_is_negative(self):
        from financials.canonical import Field
        mat = self._mat({Field.INCOME_TAX: -6518.0, Field.PRETAX_INCOME: -109295.0})
        ref, origin = intake._tax_reference(mat)
        self.assertEqual(origin, intake.ORIGIN_EXTERNAL)
        self.assertIn("不适用", ref)

    def test_effective_tax_rate_when_lines_are_missing(self):
        ref, origin = intake._tax_reference(self._mat())
        self.assertEqual(origin, intake.ORIGIN_EXTERNAL)
        self.assertIn("材料里没有", ref)

    def test_implied_debt_cost_when_available(self):
        from financials.canonical import Field
        mat = self._mat({Field.INTEREST_EXPENSE: 800.0},
                        {Field.SHORT_TERM_DEBT: 6000.0, Field.LONG_TERM_DEBT: 2000.0})
        ref, origin = intake._debt_cost_reference(mat)
        self.assertEqual(origin, intake.ORIGIN_DERIVED)
        self.assertIn("10.00%", ref)          # 800 ÷ 8000
        self.assertIn("请确认", ref)

    def test_debt_cost_says_nothing_when_interest_is_missing(self):
        ref, origin = intake._debt_cost_reference(self._mat())
        self.assertEqual(origin, intake.ORIGIN_EXTERNAL)
        self.assertIn("材料里没有利息费用", ref)
        self.assertIn("LPR", ref)             # 告诉人该填什么，而不是空着

    def test_debt_cost_says_nothing_when_debt_is_zero(self):
        from financials.canonical import Field
        ref, _ = intake._debt_cost_reference(self._mat({Field.INTEREST_EXPENSE: 5.0}))
        self.assertIn("算不出隐含利率", ref)

    def test_debt_reference_lists_the_components(self):
        from financials.canonical import Field
        mat = self._mat(bal={Field.SHORT_TERM_DEBT: 6000.0,
                             Field.LONG_TERM_DEBT: 2000.0})
        ref = intake._debt_reference(mat)
        self.assertIn("短期借款 6,000", ref)
        self.assertIn("长期借款 2,000", ref)

    def test_negative_interest_gives_no_implied_rate(self):
        """负的利息费用（取到净利息收入、或错行）→ **不给隐含利率**。

        实测：一份上市公司年报里「利息费用」有 -74 与 214 两个取值
        （合并表 / 母公司表），取到 -74 时算出的债务成本是 **-0.41%**——
        一个挂着"请确认"的荒谬数，最容易被直接抄进假设。
        """
        from financials.canonical import Field
        mat = self._mat({Field.INTEREST_EXPENSE: -74.0},
                        {Field.SHORT_TERM_DEBT: 18112.0})
        ref, origin = intake._debt_cost_reference(mat)
        self.assertEqual(origin, intake.ORIGIN_EXTERNAL)
        self.assertIn("不合理", ref)
        self.assertIn("请按实际借款利率", ref)

    def test_absurd_tax_rate_is_refused(self):
        """90% 的有效税率不合理 —— 说明取到的行不对，不给参考值。"""
        from financials.canonical import Field
        mat = self._mat({Field.INCOME_TAX: 900.0, Field.PRETAX_INCOME: 1000.0})
        ref, origin = intake._tax_reference(mat)
        self.assertEqual(origin, intake.ORIGIN_EXTERNAL)
        self.assertIn("不合理", ref)


class TestReadText(unittest.TestCase):
    """`.pdf` 不能当字节解码 —— 否则口径判定跑在乱码上。

    实测：一份 176 页的上市公司年报因为读不到「合并资产负债表」这个标题，
    被兜底判成「单体」，而它的三张表其实是合并口径。
    """

    def test_pdf_goes_through_the_extractor(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            p.write_bytes(b"%PDF-1.6\nstream\n\x00\x01binary\x00\nendstream")
            doc = SimpleNamespace(pages=[
                SimpleNamespace(text="合并资产负债表 单位：百万元"),
                SimpleNamespace(text="资产总计 1,000")])
            with mock.patch("ingest.pdf.extract_pdf", return_value=doc):
                t = intake._read_text(p)
        self.assertIn("合并资产负债表", t)
        self.assertIn("资产总计", t)
        self.assertNotIn("%PDF", t)

    def test_pdf_that_cannot_be_read_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            p.write_bytes(b"%PDF-1.6 broken")
            with mock.patch("ingest.pdf.extract_pdf", side_effect=RuntimeError("坏了")):
                self.assertEqual(intake._read_text(p), "")

    def test_binary_dressed_as_text_returns_empty(self):
        """解出来是乱码就返回空 —— 空会让口径判成「未判定」，
        乱码会让它判出一个**看起来很具体**的错结论。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.txt"
            p.write_bytes(b"\x00" * 200 + b"%PDF-1.6 garbage")
            self.assertEqual(intake._read_text(p), "")

    def test_html_still_gets_tags_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.htm"
            p.write_text("<html><body><td>合并资产负债表</td></body></html>",
                         encoding="utf-8")
            self.assertIn("合并资产负债表", intake._read_text(p))
            self.assertNotIn("<td>", intake._read_text(p))


class TestAsPercentText(unittest.TestCase):
    """把"人只填了数值"补成带 % 的写法。规则边界在这里一条条钉死。"""

    def test_bare_number(self):
        self.assertEqual(intake.as_percent_text("8.5"), "8.5%")

    def test_every_element_of_a_list(self):
        """逗号列表要**逐个**补 —— 只补最后一个的话，前几个就是 100 倍错误。"""
        self.assertEqual(intake.as_percent_text("10, 9, 8"), "10%,9%,8%")
        self.assertEqual(intake.as_percent_text("10，9，8"), "10%,9%,8%")

    def test_already_has_sign(self):
        self.assertEqual(intake.as_percent_text("8.5%"), "8.5%")

    def test_sign_goes_before_the_source(self):
        self.assertEqual(intake.as_percent_text("8.5 @管理层 p.12"),
                         "8.5% @管理层 p.12")
        self.assertEqual(intake.as_percent_text("10,9 @管理层"), "10%,9% @管理层")

    def test_non_numbers_are_left_alone(self):
        """`数据不足` 补成 `数据不足%` 是编造 —— 原样交给下游判成缺失。"""
        self.assertEqual(intake.as_percent_text("数据不足"), "数据不足")
        self.assertEqual(intake.as_percent_text("待定"), "待定")

    def test_negative_and_leading_dot(self):
        self.assertEqual(intake.as_percent_text("-2.5"), "-2.5%")
        self.assertEqual(intake.as_percent_text(".5"), ".5%")

    def test_empty_stays_empty(self):
        self.assertEqual(intake.as_percent_text(""), "")
        self.assertEqual(intake.as_percent_text("   "), "")

    def test_list_values_become_the_right_numbers(self):
        """列表逐项换算的最后一道闸：5,4.5,4 → 0.05 / 0.045 / 0.04。"""
        mat = intake.scan(str(FITBIT))
        ans = {"growth": intake.parse_answer("5, 4.5, 4")}
        intake.normalize_percents(mat, ans)
        self.assertEqual(intake._lst(ans["growth"].value), ["5%", "4.5%", "4%"])
        self.assertEqual([s["value"] for s in intake._specs(ans["growth"])],
                         [0.05, 0.045, 0.04])

    def test_normalize_only_touches_percent_keys(self):
        mat = intake.scan(str(FITBIT))
        ans = {"risk_free": intake.parse_answer("2.45"),
               "beta_unlevered": intake.parse_answer("1.10")}
        fixed = intake.normalize_percents(mat, ans)
        self.assertEqual(fixed, ["risk_free"])              # 只动百分比字段
        self.assertEqual(ans["risk_free"].value, "2.45%")
        self.assertEqual(ans["beta_unlevered"].value, "1.10")   # beta 是倍数，不是百分比

    def test_empty_answer_is_skipped(self):
        mat = intake.scan(str(FITBIT))
        ans = {"growth": intake.parse_answer("")}
        self.assertEqual(intake.normalize_percents(mat, ans), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
