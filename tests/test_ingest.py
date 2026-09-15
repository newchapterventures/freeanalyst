"""输入层测试 —— PDF 抽取 + 文本归一化。

## 这里锁的是「静默失败」

PDF 抽出来的中文，码位经常不是你以为的那个：

    货币资⾦   ⾦ 是 U+2FA6（康熙部首金），不是 金 U+91D1
    ⻓期借款   ⻓ 是 U+2ED3（CJK RADICAL LONG），不是 长 U+957F

**看着一样，`==` 比较是 False。** 后果是科目名对不上、检索命不中 ——
而且不报错。整条流水线看起来在跑，结果是错的。

两份测试 PDF 的取材：`corpus/test-financials.pdf` 由
`corpus/test-financials.html` 用浏览器渲染而成，用的是示例语料里的数字。
**不含任何真实交易材料。**
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingest import normalize as nz  # noqa: E402
from ingest import pdf as ip  # noqa: E402

PDF = ROOT / "corpus" / "test-financials.pdf"
HTML = ROOT / "corpus" / "test-financials.html"


class TestNormalizeBasics(unittest.TestCase):
    def test_nfkc_handles_kangxi_radicals(self):
        """康熙部首有 compatibility 分解，NFKC 能修。"""
        for bad, good in [("货币资⾦", "货币资金"), ("项⽬", "项目"),
                          ("⽣产", "生产"), ("⼈员", "人员")]:
            with self.subTest(bad=bad):
                self.assertNotEqual(bad, good, "前提：这俩本来就不相等")
                self.assertEqual(nz.normalize_text(bad), good)

    def test_manual_table_handles_what_nfkc_cannot(self):
        """**这批 NFKC 修不了。**

        U+2E80–U+2EFF 里有 113 个字符没有 NFKC 映射。实测漏过两个：
        ⻓（长期借款）和 ⺠（民营）。
        """
        for bad, good in [("⻓期借款", "长期借款"), ("⺠营企业", "民营企业"),
                          ("⻔", "门"), ("⻢", "马"), ("⻛险", "风险"),
                          ("⻚码", "页码"), ("⻋辆", "车辆")]:
            with self.subTest(bad=bad):
                self.assertEqual(unicodedata.normalize("NFKC", bad), bad,
                                 "前提：NFKC 对它无效")
                self.assertEqual(nz.normalize_text(bad), good)

    def test_idempotent(self):
        """幂等 —— 反复入库不能让文本漂移。"""
        s = "货币资⾦ ⻓期借款 ⺠营企业 （一）"
        once = nz.normalize_text(s)
        self.assertEqual(nz.normalize_text(once), once)

    def test_fullwidth_punctuation_is_normalized(self):
        self.assertEqual(nz.normalize_text("（一）"), "(一)")
        self.assertEqual(nz.normalize_text("：，"), ":,")

    def test_empty_and_none_safe(self):
        self.assertEqual(nz.normalize_text(""), "")
        self.assertIsNone(nz.normalize_text(None))

    def test_numbers_are_untouched(self):
        """归一化不许改数字 —— 证据规则要求数字原样引用。"""
        for s in ["3,180", "18,300", "-1,120", "23.0%", "2025-12-31"]:
            with self.subTest(s=s):
                self.assertEqual(nz.normalize_text(s), s)


class TestCompatReporting(unittest.TestCase):
    def test_reports_fixed_chars(self):
        issues = nz.compat_issues("货币资⾦ ⻓期借款 （一）")
        self.assertIn("⾦", issues)
        self.assertIn("⻓", issues)
        self.assertIn("（", issues)

    def test_clean_text_reports_nothing(self):
        self.assertEqual(nz.compat_issues("货币资金"), [])
        self.assertIn("无兼容性字符", nz.describe_compat_issues("货币资金"))

    def test_unfixable_chars_are_reported_not_guessed(self):
        """**猜错比不处理更糟。**

        纯偏旁形式的部首（亻氵扌衤⺅）不属于「部首形即正字」那一类，
        我没有把握它们该对应哪个正字，所以报出来让人判断。

        注意 `⺮`（BAMBOO）**在**映射表里 —— 它的字形就是完整的「竹」。
        这个测试用过它，是写错了。
        """
        unknown = nz.unfixable_issues("这里有个 ⺅ 和 ⺂")
        self.assertIn("\u2e85", unknown, "⺅ PERSON 是纯偏旁，应报出来")
        self.assertIn("\u2e82", unknown, "⺂ SECOND ONE 是纯偏旁，应报出来")
        self.assertNotIn("\u2eae", unknown, "⺮ 已映射到 竹，不该出现在未映射里")

    def test_mapped_chars_are_not_reported_as_unmapped(self):
        """⺮（竹）、⻓（长）、⺠（民）都在表里，归一化后不该再报警。"""
        self.assertEqual(nz.unfixable_issues("⻓ ⺠ ⻔ ⺮ ⻤"), [])
        self.assertEqual(nz.normalize_text("⺮ ⻤"), "竹 鬼")

    def test_describe_mentions_both_categories(self):
        text = nz.describe_compat_issues("⻓ ⺅")
        self.assertIn("已归一化", text)
        self.assertIn("没有映射", text)


class TestPdfExtraction(unittest.TestCase):
    """需要 `corpus/test-financials.pdf`。缺了就现场从 HTML 生成。"""

    @classmethod
    def setUpClass(cls):
        if not PDF.exists():
            raise unittest.SkipTest(
                f"缺少测试 PDF：{PDF}\n"
                f"（源文件在 {HTML.name}，用浏览器打印成 PDF；见 tests/README 说明）"
            )

    def test_page_and_table_counts(self):
        doc = ip.extract_pdf(PDF)
        self.assertEqual(doc.page_count, 2)
        self.assertEqual(sum(len(p.tables) for p in doc.pages), 5)

    def test_balance_sheet_is_a_grid_not_flowing_text(self):
        """**表格结构是本模块存在的理由。**

        pdftotext 会把资产负债表抽成流式文本，行列对应就丢了 ——
        "3,180" 到底是货币资金还是应收账款，从文本里看不出来。
        """
        doc = ip.extract_pdf(PDF)
        t = doc.pages[0].tables[0]
        self.assertGreater(len(t), 15)
        self.assertEqual(len(t[0]), 4)      # 项目 + 三个年度

    def test_account_names_are_exactly_matchable(self):
        """这是归一化存在的全部意义。**不归一化这批断言全挂。**"""
        doc = ip.extract_pdf(PDF)
        names = [r[0] for r in doc.pages[0].tables[0] if r[0]]
        for probe in ["货币资金", "应收账款", "长期借款", "资产总计",
                      "负债和所有者权益总计"]:
            with self.subTest(probe=probe):
                self.assertIn(probe, names,
                              f"『{probe}』匹配不上 —— 大概率是字符码位没归一化")

    def test_numbers_survive_extraction(self):
        doc = ip.extract_pdf(PDF)
        t = doc.pages[0].tables[0]
        row = next(r for r in t if r[0] == "货币资金")
        self.assertEqual(row[1], "3,180")
        self.assertEqual(row[2], "2,640")
        self.assertEqual(row[3], "1,980")

    def test_balance_sheet_foots(self):
        """勾稽自检：资产 = 负债 + 所有者权益。

        这个测试同时验证两件事：抽取没串行，以及归一化没改动数字。
        """
        doc = ip.extract_pdf(PDF)
        t = doc.pages[0].tables[0]
        get = lambda name: next(  # noqa: E731
            int(r[1].replace(",", "")) for r in t if r[0] == name)

        assets = get("资产总计")
        liab = get("负债合计")
        equity = get("所有者权益合计")
        self.assertEqual(assets, liab + equity,
                         f"资产 {assets} ≠ 负债 {liab} + 权益 {equity}")

    def test_text_has_page_markers(self):
        """尽调结论要能指到「哪一页」。"""
        doc = ip.extract_pdf(PDF)
        text = doc.to_text()
        self.assertIn("【第 1 页】", text)
        self.assertIn("【第 2 页】", text)

    def test_tables_rendered_as_markdown(self):
        doc = ip.extract_pdf(PDF)
        md = doc.pages[0].tables_markdown()
        self.assertIn("| 货币资金 | 3,180 |", md)

    def test_summary_reports_the_normalization(self):
        doc = ip.extract_pdf(PDF)
        s = doc.summary()
        self.assertIn("归一化修正了", s)
        self.assertIn("静默失配", s)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            ip.extract_pdf(ROOT / "corpus" / "根本不存在.pdf")

    def test_compat_issues_after_normalization_is_empty(self):
        """抽出来的文本已经归一化，不该再报兼容性问题。"""
        doc = ip.extract_pdf(PDF)
        self.assertEqual(doc.unmapped_chars, [],
                         "有部首字符没被处理 —— 要么补映射表，要么报出来")
        self.assertTrue(doc.compat_chars, "这份 PDF 本来就有兼容性字符，应该报出来")


class TestDependencyMessage(unittest.TestCase):
    def test_missing_dependency_message_is_actionable(self):
        """没装 pdfplumber 时要说清楚「怎么装」和「不装也能用」。"""
        import inspect
        src = inspect.getsource(ip._import_pdfplumber)
        self.assertIn("pip install pdfplumber", src)
        self.assertIn("不装也不影响其他功能", src)


class TestCiteLabel(unittest.TestCase):
    """引用标签 —— 实测模型把段落号误读成了页码，所以这里锁死。"""

    def _chunk(self, text, para=1, source="x.pdf"):
        from retrieval import Chunk
        return Chunk("S1", source, para, para + 1, text)

    def test_page_marker_gives_a_page(self):
        c = self._chunk("【第 1 页】\n资产负债表")
        self.assertEqual(c.page_range(), (1, 1))
        self.assertEqual(c.cite_label(), "x.pdf 第 1 页")

    def test_table_header_page_is_recognized(self):
        """**实测踩过**：正则原来要求 `】` 紧跟 `页`，表头里的页码全认不出来。"""
        c = self._chunk("【第 2 页 · 表 3】\n| 项目 | 值 |")
        self.assertEqual(c.page_range(), (2, 2))
        self.assertEqual(c.cite_label(), "x.pdf 第 2 页")

    def test_multi_page_chunk_gives_a_range(self):
        c = self._chunk("【第 1 页】\n甲\n【第 2 页】\n乙")
        self.assertEqual(c.page_range(), (1, 2))
        self.assertEqual(c.cite_label(), "x.pdf 第 1–2 页")

    def test_no_marker_falls_back_to_paragraph(self):
        """没有页码就报段落 —— **不用容易误读的 `#pN`**。"""
        c = self._chunk("普通段落", para=5, source="材料.txt")
        self.assertIsNone(c.page_range())
        self.assertEqual(c.cite_label(), "材料.txt 第 5 段")
        self.assertNotIn("#p", c.cite_label())

    def test_range_marker_with_dash(self):
        c = self._chunk("【第 3–4 页】\n内容")
        self.assertEqual(c.page_range(), (3, 4))


class TestTxtAlsoNormalized(unittest.TestCase):
    """**只归一化 PDF 是不够的。**

    索引侧和查询侧口径不一致就会静默失配，比不归一化还隐蔽。
    """

    def test_load_material_normalizes_txt(self):
        import tempfile
        from freeanalyst import _load_material

        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8",
                                         delete=False) as f:
            f.write("货币资⾦ ⻓期借款 （一）")
            p = Path(f.name)
        try:
            text, _ = _load_material(p)
        finally:
            p.unlink()
        self.assertEqual(text, "货币资金 长期借款 (一)")
        self.assertNotIn("\u2fa6", text)


if __name__ == "__main__":
    unittest.main()
