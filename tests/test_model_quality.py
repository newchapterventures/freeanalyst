"""评测器自身的测试。

## 为什么评测器需要被测试

**假阴性比没有评测更危险。**

实测踩到的：`check_subject` 把

    回购义务由实控人个人承担，公司不承担，因此对公司无财务压力。

判成了「把个人义务说成公司负担」—— 这是**完全正确**的答案。
原因是正则匹配到了 `公司…财务…压力`，而它不认否定词 `无`。

如果我当时直接信了这个结果，结论会是「30B 模型在主体识别上也不行」，
从而淘汰一个其实答对了的模型。反过来，一个宽松到什么都过的评测器，
会让真正危险的模型看起来没问题。

所以这里把「哪些表述该判错、哪些该判对」逐条钉死。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))

import model_quality as mq  # noqa: E402


class TestCompanyBurdenNegation(unittest.TestCase):
    """否定形式是正确答案，不能判错。"""

    def test_negated_pressure_is_not_an_error(self):
        """实测中真正误判过的那一句。"""
        text = "回购义务由实控人个人承担，公司不承担，因此对公司无财务压力。"
        self.assertIsNone(mq.find_company_burden(text))

    def test_genuine_burden_is_caught(self):
        """这是 7B 真实犯的错，必须判出来。"""
        text = "该回购义务使公司面临较大的财务压力。"
        self.assertIsNotNone(mq.find_company_burden(text))

    def test_various_negation_forms(self):
        for text in [
            "对公司不构成财务压力",
            "不产生财务负担",
            "对公司没有财务影响",
            "不存在财务冲击",
            "公司不承担，故无资金压力",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(mq.find_company_burden(text))

    def test_various_positive_forms(self):
        for text in [
            "公司面临较大的财务压力",
            "对公司的现金流造成重大影响",
            "会加重公司的资金负担",
            "给公司带来偿债压力",
            # 实测漏过的一种：中间夹的是「回购」而不是「财务」
            "若未完成 IPO，公司将面临回购压力",
            "公司需承担回购义务",
        ]:
            with self.subTest(text=text):
                self.assertIsNotNone(mq.find_company_burden(text))

    def test_company_buyback_burden_is_caught(self):
        """gemma3 真实犯的错：结论节写对了，风险提示节写错了。"""
        answer = (
            "## 结论\n回购义务人为实控人个人，承担无限连带责任 [S3]。\n\n"
            "## 材料缺口\n具体的回购金额是多少？\n\n"
            "## 风险提示\n若未完成 IPO，公司将面临回购压力，实控人需承担无限连带责任 [S3, S5]。\n"
        )
        reasons = mq.check_subject(answer, [])
        self.assertTrue(
            any("公司负担" in r for r in reasons),
            f"风险提示节的主体搞混必须被抓到，实际判分：{reasons}",
        )

    def test_mixed_text_finds_the_real_one(self):
        """同一段里既有否定也有肯定时，要抓到肯定的那个。"""
        text = "对公司无财务压力。但会加重公司的资金负担。"
        self.assertIsNotNone(mq.find_company_burden(text))


class TestNonCompanyObligationPatterns(unittest.TestCase):
    """「义务不落在公司」的说法有很多种，漏一种就把正确答案判错。"""

    def test_common_phrasings_are_recognized(self):
        for text in [
            "该义务不落在公司身上",
            "不构成公司的义务",
            "由实控人个人承担",
            "公司不承担该项义务",
            "不属于公司的债务",
        ]:
            with self.subTest(text=text):
                self.assertIsNotNone(
                    mq._NON_COMPANY_OBLIGATION.search(text),
                    f"{text!r} 应该被认作「义务不在公司」，漏了就会把正确答案判错",
                )


class TestSubjectCheck(unittest.TestCase):
    def test_correct_answer_passes(self):
        """实测中 30B 给的那个答案，修正后必须通过。"""
        answer = (
            "## 结论\n"
            "回购义务由实控人个人承担，公司不承担，因此对公司无财务压力。[S2][S3]\n\n"
            "## 材料缺口\n未提供回购义务的具体金额与支付时间点。\n\n"
            "## 风险提示\n未发现\n"
        )
        self.assertEqual(mq.check_subject(answer, []), [])

    def test_wrong_answer_still_fails(self):
        """7B 的错答必须继续失败 —— 修正不能顺带放过它。"""
        answer = (
            "## 结论\n该回购义务使公司面临较大的财务压力。[S1]\n\n"
            "## 材料缺口\n未提供。\n\n"
            "## 风险提示\n未发现\n"
        )
        reasons = mq.check_subject(answer, [])
        self.assertTrue(any("公司负担" in r for r in reasons))

    def test_empty_conclusion_is_not_a_pass(self):
        self.assertTrue(mq.check_subject("## 材料缺口\n未提供\n", []))


class TestSectionSplitting(unittest.TestCase):
    def test_splits_three_sections(self):
        answer = "## 结论\nA\n\n## 材料缺口\nB\n\n## 风险提示\nC\n"
        secs = mq.split_sections(answer)
        self.assertEqual(secs["结论"].strip(), "A")
        self.assertEqual(secs["材料缺口"].strip(), "B")
        self.assertEqual(secs["风险提示"].strip(), "C")

    def test_missing_sections_are_empty_not_keyerror(self):
        secs = mq.split_sections("没有任何标题的答案")
        self.assertEqual(secs, {"结论": "", "材料缺口": "", "风险提示": ""})

    def test_empty_answer(self):
        secs = mq.split_sections("")
        self.assertTrue(all(v == "" for v in secs.values()))


class TestFormatCheck(unittest.TestCase):
    def test_perfunctory_gap_section_fails(self):
        """只写「材料未提供」四个字不算列出了缺口。"""
        answer = "## 结论\nA\n\n## 材料缺口\n材料未提供\n\n## 风险提示\nC\n"
        reasons = mq.check_format(answer, [])
        self.assertTrue(any("敷衍" in r or "只写了" in r for r in reasons))

    def test_empty_output_fails_all_sections(self):
        """30B 在两个用例上返回空字符串 —— 必须是明确失败，不能蒙混。"""
        reasons = mq.check_format("", [])
        self.assertEqual(len(reasons), 3)


class TestHallucinationCheck(unittest.TestCase):
    def test_fabricated_conclusion_is_caught(self):
        """7B 真实犯的错。"""
        answer = "## 结论\n该问题已闭环，不会再发生。[S1]\n\n## 材料缺口\n无\n\n## 风险提示\n无\n"
        reasons = mq.check_no_hallucination(answer, [])
        self.assertTrue(any("编造" in r for r in reasons))

    def test_admitting_missing_passes(self):
        answer = "## 结论\n材料未提供该确认函。[S1]\n\n## 材料缺口\n无\n\n## 风险提示\n无\n"
        self.assertEqual(mq.check_no_hallucination(answer, []), [])


if __name__ == "__main__":
    unittest.main()
