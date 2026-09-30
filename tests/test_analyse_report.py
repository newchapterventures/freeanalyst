"""财务分析报告 —— 钉住四件事。

用**真的类**（`StatementSet` / `Statements`）+**假数据**：
上一轮有个测试自己造了假想的结构，量出来的是假绿灯 —— 这次不重犯。
"""
from __future__ import annotations

import unittest

from financials.canonical import Field
from financials.statements import StatementRow, StatementSet, Statements
from valuation.analyse_report import build_report
from valuation.dupont import analyse as dupont


def _bal(with_total: bool = True) -> StatementSet:
    rows = [
        StatementRow("货币资金", 143319.0, Field.CASH, "pdf"),
        StatementRow("负债合计", 6982611.0, Field.TOTAL_LIABILITIES, "pdf"),
        StatementRow("股东权益合计", 608393.0, Field.EQUITY, "pdf"),
    ]
    if with_total:
        rows.insert(0, StatementRow("资产总计", 7591004.0, Field.TOTAL_ASSETS, "pdf"))
    return StatementSet(name="资产负债表", source="pdf", unit="百万元",
                        rows=rows,
                        fields={r.field: r.value for r in rows
                                if r.field and r.value is not None},
                        columns=["第89—91页"])


def _inc() -> StatementSet:
    rows = [
        StatementRow("一、营业收入", 615678.0, Field.REVENUE, "pdf"),
        StatementRow("五、净利润", 156552.0, Field.NET_INCOME, "pdf"),
        StatementRow("利润总额", 181629.0, Field.PRETAX_INCOME, "pdf"),
    ]
    return StatementSet(name="利润表", source="pdf", unit="百万元", rows=rows,
                        fields={r.field: r.value for r in rows
                                if r.field and r.value is not None},
                        columns=["第93—93页"])


def _report(bal: StatementSet | None = None) -> str:
    st = Statements(balance=bal if bal is not None else _bal(), income=_inc(),
                    cash_flow=None)
    return build_report(st, dupont(st), target="某材料",
                        generated_at="2026-09-30 10:00", version="0.43")


class TestAnalyseReport(unittest.TestCase):
    def test_has_all_sections(self) -> None:
        txt = _report()
        for sec in ("财务分析报告", "一、材料与口径", "二、经营回报",
                    "三、输入与来源", "四、提示"):
            self.assertIn(sec, txt)
        self.assertIn("材料：某材料", txt)

    def test_every_input_carries_its_provenance(self) -> None:
        """四个数都要写清取自哪张表、原表科目名 —— 出处可追溯是硬标准。"""
        txt = _report()
        self.assertIn("资产总计", txt)
        self.assertIn("利润表 · 原表科目「一、营业收入」", txt)
        self.assertIn("资产负债表 · 原表科目「股东权益合计」", txt)

    def test_says_not_applicable_when_a_number_is_missing(self) -> None:
        """缺数时**明说不适用**，并且不硬凑 —— 这是这个工具的底线。"""
        txt = _report(bal=_bal(with_total=False))
        self.assertIn("不适用", txt)
        self.assertIn("总资产", txt)

    def test_carries_no_valuation_assumptions(self) -> None:
        """财务分析**不含任何假设** —— 估值专用的词一个都不该出现。"""
        txt = _report()
        for word in ("无风险利率", "折现率", "永续增长", "beta", "WACC"):
            self.assertNotIn(word, txt, f"财务分析报告里不该出现「{word}」")

    def test_says_no_assumptions_in_the_disclaimer(self) -> None:
        self.assertIn("不含任何假设", _report())

    def test_missing_key_fields_are_shown(self) -> None:
        """关键科目缺口要写在口径里（不是"映射率"那种会被表头灌满的指标）。"""
        self.assertIn("关键科目缺口", _report())


if __name__ == "__main__":
    unittest.main()
