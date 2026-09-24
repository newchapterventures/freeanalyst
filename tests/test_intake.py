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
            rc = intake.appraise(dummy)
            self.assertEqual(rc, 0)
            self.assertTrue((dummy / "估值问答.txt").exists())
            # 单位认不出来时必须**写在清单里**让人看见
            self.assertIn("必须你声明", (dummy / "估值问答.txt").read_text(encoding="utf-8"))

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
            rc = intake.appraise(dummy, ans)
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

    def test_template_lands_next_to_the_file(self):
        """清单放在材料旁边，**带上文件名前缀** —— 否则同目录两份材料会互相覆盖。"""
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
            self.assertEqual(intake.appraise(src), 0)
            self.assertTrue((Path(d) / "某标的年报-估值问答.txt").exists())

    def test_missing_path_says_so(self):
        with self.assertRaises(FileNotFoundError):
            intake.scan("/tmp/根本没有这个文件.pdf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
