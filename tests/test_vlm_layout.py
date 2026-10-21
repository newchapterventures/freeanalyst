"""视觉模型判版面那一层的测试。

**不调真模型** —— 测的是判据：解析、开关、三道闸、以及「切错了要退回」。

★ 这一版实测没跑通（见 `ingest/vlm.py` 顶部的记录），所以最要紧的用例是
「**关掉时行为一个字节都不许变**」—— 不能让一个没修好问题、还慢 6 倍的
东西留在默认路径上。
"""

from __future__ import annotations

import unittest

from ingest import ocr, vlm


def _rows(*specs):
    """`(x, 文本)` 的若干行 → OCR 的行结构（补一个顺序 y，测试里不需要真坐标）。"""
    return [[(x, float(i), t) for i, (x, t) in enumerate(r)] for r in specs]


def _cut(answer: str) -> float:
    """解析出切点；解析不出来就让用例失败（而不是让类型检查报一堆）。"""
    got = vlm._parse(answer)
    assert got is not None, f"应当解析出切点，实际是 None：{answer!r}"
    return got


class TestParse(unittest.TestCase):
    """模型回答 → 切点。**读不准就返回 None，绝不猜。**"""

    def test_plain_number(self):
        self.assertAlmostEqual(_cut("50"), 0.5)

    def test_percent_sign(self):
        self.assertAlmostEqual(_cut("52%"), 0.52)

    def test_decimal(self):
        self.assertAlmostEqual(_cut("50.5"), 0.505)

    def test_single_column_says_no(self):
        """明确说单栏 → 不切。"""
        self.assertIsNone(vlm._parse("单栏"))

    def test_single_column_inside_sentence(self):
        self.assertIsNone(vlm._parse("这一页是单栏的"))

    def test_empty(self):
        self.assertIsNone(vlm._parse(""))

    def test_nonsense(self):
        self.assertIsNone(vlm._parse("看不清"))

    def test_too_far_left_rejected(self):
        """太靠边说明它在胡说 —— 宁可判「不知道」，也不能乱切。"""
        self.assertIsNone(vlm._parse("10"))
        self.assertIsNone(vlm._parse("24"))

    def test_too_far_right_rejected(self):
        self.assertIsNone(vlm._parse("76"))
        self.assertIsNone(vlm._parse("95"))

    def test_multiple_numbers_rejected(self):
        """它开始解释/抄数字 → 不采信。"""
        self.assertIsNone(vlm._parse("左边 50，右边 200"))

    def test_boundaries_accepted(self):
        self.assertAlmostEqual(_cut("25"), 0.25)
        self.assertAlmostEqual(_cut("75"), 0.75)


class TestSplitWithExternalCut(unittest.TestCase):
    """`split_two_sided(cut_x=…)` —— 给外部切点这条路。"""

    def setUp(self):
        self.rows = _rows(
            [(0.10, "货币资金"), (0.35, "1,000.00"), (0.50, "短期借款"), (0.75, "2,000.00")],
            [(0.10, "应收账款"), (0.35, "500.00"), (0.50, "应付账款"), (0.75, "300.00")],
        )

    def test_external_cut_used(self):
        blocks = ocr.split_two_sided(self.rows, cut_x=0.45)
        self.assertEqual(len(blocks), 2)
        left = [t for r in blocks[0] for _x, _y, t in r]
        right = [t for r in blocks[1] for _x, _y, t in r]
        self.assertIn("货币资金", left)
        self.assertIn("短期借款", right)

    def test_cut_returns_cells_on_both_sides(self):
        blocks = ocr.split_two_sided(self.rows, cut_x=0.45)
        self.assertTrue(all(len(b) > 0 for b in blocks))

    def test_no_cut_still_works(self):
        """不传切点 → 老行为（几何算），不崩。"""
        blocks = ocr.split_two_sided(self.rows)
        self.assertGreaterEqual(len(blocks), 1)


class TestAmountCount(unittest.TestCase):
    def test_counts_amount_cells(self):
        rows = _rows([(0.1, "1,000.00"), (0.2, "科目"), (0.3, "500")])
        self.assertEqual(ocr._amount_count(rows), 2)

    def test_zero_when_no_amounts(self):
        rows = _rows([(0.1, "货币资金"), (0.2, "应收账款")])
        self.assertEqual(ocr._amount_count(rows), 0)


class TestDensityGate(unittest.TestCase):
    """第三道闸：正文页不花那 1–2 分钟去问模型。"""

    def test_dense_page_passes(self):
        rows = _rows(*[[(0.1 + i * 0.01, f"{i},000.00")] for i in range(12)])
        self.assertTrue(vlm._is_dense_with_numbers(rows))

    def test_sparse_page_blocked(self):
        rows = _rows([(0.1, "本公司注册资本")], [(0.1, "1,000 万元")], [(0.1, "见附注")])
        self.assertFalse(vlm._is_dense_with_numbers(rows))

    def test_empty_blocked(self):
        self.assertFalse(vlm._is_dense_with_numbers([]))


class TestDisabledByDefault(unittest.TestCase):
    """★ **这一版默认必须是关的** —— 它没修好问题，还慢 6 倍。"""

    def test_default_is_off(self):
        self.assertFalse(vlm.ENABLED,
                         "这一版没有跑通，默认必须关；打开用 FREANALYST_VLM_LAYOUT=1")

    def test_disabled_means_never_called(self):
        self.assertIsNone(vlm.propose_cut(
            _rows(*[[(0.1, f"{i},000.00")] for i in range(20)]),
            "/tmp/不存在.pdf", 1))

    def test_rows_to_table_with_pdf_still_works_when_off(self):
        """关掉时，传了 pdf/page 也**不能**改变行为。"""
        rows = _rows([(0.10, "货币资金"), (0.35, "1,000.00"), (0.50, "科目"), (0.75, "2,000.00")])
        a = ocr.rows_to_table(rows)
        b = ocr.rows_to_table(rows, pdf="/tmp/不存在.pdf", page=1)
        self.assertEqual(a, b)


class TestAvailable(unittest.TestCase):
    def test_unavailable_host_is_false_not_raise(self):
        """连不上就是 False —— 不许抛出去把主流程带崩。"""
        old = vlm.HOST
        vlm.HOST = "http://127.0.0.1:9"      # 没人监听
        vlm._AVAILABLE = None
        try:
            self.assertFalse(vlm.available())
        finally:
            vlm.HOST = old
            vlm._AVAILABLE = None

    def test_result_is_cached(self):
        """探一次就够 —— 否则每页都去敲一次 /api/tags。"""
        old = vlm.HOST
        vlm.HOST = "http://127.0.0.1:9"
        vlm._AVAILABLE = None
        try:
            vlm.available()
            self.assertIs(vlm._AVAILABLE, False)
        finally:
            vlm.HOST = old
            vlm._AVAILABLE = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
