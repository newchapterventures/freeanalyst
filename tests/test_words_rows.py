"""按坐标配对（`ingest/layout.parse_words_rows`）—— 用**实测几何**钉住。

## 这里的坐标全部来自真实材料（中国人寿 2025 年报第 89 页，公开来源）

    科目名            x≈62–72   中心 y = 145.2
    附注号            x≈367     中心 y = 138.0
    本期金额          x≈430     中心 y = 138.0
    上期金额          x≈530     中心 y = 138.0
    下一行的金额                 中心 y = 153.0
    下一行的科目名               中心 y = 160.2

**金额行比它的科目行高约 7 点**（金额格垂直居中）—— 所以按 y 从上到下读，
文字流是"金额在前、科目名在后"，纯文本规则会整表错配一位。

⚠️ 教训：之前有个测试**自己造了假想的结构**，于是量出的"全绿"是假的。
这里的每个数字都是从真材料上量下来的。
"""
from __future__ import annotations

import unittest

from ingest.layout import looks_like_split_rows, parse_words_rows


def w(text: str, x0: float, top: float, h: float = 6.0) -> dict:
    """造一个词 —— 形状与 `pdfplumber.extract_words()` 一致。"""
    return {"text": text, "x0": x0, "top": top, "bottom": top + h}


class TestWordsRows(unittest.TestCase):
    def test_amount_row_above_label_row_pairs_downwards(self) -> None:
        """★ 核心：金额行在**上面**时，要配给**下面**那个科目（不是上面那个）。"""
        words = [
            w("资产：", 62, 123.1),
            w("1", 367, 135.0), w("143,319", 430, 135.0), w("86,519", 530, 135.0),
            w("货币资金", 71, 142.2),
            w("2", 367, 150.0), w("50,879", 445, 150.0), w("30,560", 530, 150.0),
            w("买入返售金融资产", 71, 157.2),
        ]
        rows = parse_words_rows(words)
        got = {r[0]: (r[1], r[2], r[3]) for r in rows}
        self.assertEqual(got["货币资金"], ("1", "143,319", "86,519"))
        self.assertEqual(got["买入返售金融资产"], ("2", "50,879", "30,560"))

    def test_header_line_does_not_steal_an_amount_row(self) -> None:
        """表头/页眉离金额行远 —— 不许把金额行吃掉（否则整表错开一位）。"""
        words = [
            w("资产", 62, 103.5), w("附注十", 356, 103.5), w("年月日", 425, 103.5),
            w("1", 367, 135.0), w("143,319", 430, 135.0), w("86,519", 530, 135.0),
            w("货币资金", 71, 142.2),
        ]
        rows = parse_words_rows(words)
        got = {r[0]: r[2] for r in rows}
        self.assertEqual(got.get("货币资金"), "143,319")

    def test_section_header_is_kept_but_takes_no_value(self) -> None:
        """段标题「资产：」要留在结果里（人能看见），但**不许占金额行**。"""
        words = [
            w("资产：", 62, 123.1),
            w("1", 367, 135.0), w("143,319", 430, 135.0), w("86,519", 530, 135.0),
            w("货币资金", 71, 142.2),
        ]
        rows = parse_words_rows(words)
        headers = [r for r in rows if r[0] == "资产："]
        self.assertTrue(headers, "段标题不该被丢掉")
        self.assertEqual(headers[0][2], "", "段标题不该占金额")
        self.assertEqual(next(r[2] for r in rows if r[0] == "货币资金"), "143,319")

    def test_note_is_recognised_by_horizontal_gap(self) -> None:
        """有附注列时（与金额拉开一大段空白）才认它 —— 判据是**横向距离**。"""
        words = [
            w("货币资金", 71, 142.2),
            w("1", 367, 135.0), w("143,319", 430, 135.0), w("86,519", 530, 135.0),
        ]
        rows = parse_words_rows(words)
        self.assertEqual(rows[0][1], "1")
        self.assertEqual(rows[0][2], "143,319")
        self.assertEqual(rows[0][3], "86,519")

    def test_single_period_value_lands_in_the_last_slot(self) -> None:
        """只有一个金额列时，值落在**最后一个槽位**（= 上期位置）。

        这是与 `parse_layout_lines` 一致的既有约定（行尾对齐到 `max_values` 列）。
        **显式钉住它** —— 免得下次有人以为单列的表也会落在"本期"位上。
        真实材料的三张表都是两列（本期 + 上期），所以取数不受影响。
        """
        rows = parse_words_rows([w("货币资金", 71, 142.2), w("143,319", 430, 135.0)])
        self.assertEqual(rows[0][1], "")
        self.assertEqual(rows[0][2], "")
        self.assertEqual(rows[0][3], "143,319")


def _split_page() -> list[dict]:
    """错位版式：标签行与金额行**分开**，金额行比标签行高 7 点（实测形状）。"""
    out = []
    for i in range(10):
        y = 135.0 + i * 15
        out += [w(f"{i + 1}", 367, y), w(f"{i}00,000", 430, y), w(f"{i}00,001", 530, y)]
        out.append(w(f"科目{i}", 71, y + 7.2))
    return out


def _normal_page() -> list[dict]:
    """正常版式：标签与金额**在同一行**（实测形状）。"""
    out = []
    for i in range(10):
        y = 135.0 + i * 15
        out += [w(f"科目{i}", 71, y), w(f"{i}", 367, y),
                w(f"{i}00,000", 430, y), w(f"{i}00,001", 530, y)]
    return out


class TestLayoutGate(unittest.TestCase):
    """要不要按坐标配对，由**版式本身**决定 —— 不是由"结果好不好"决定。

    实测（两份公开来源材料）：
        错位版式：纯金额行占 **43–44%**，标签与金额同行的只有 1 行
        正常版式：纯金额行占 **2%**，标签与金额同行 18–20 行

    我第一版拿"映射行更多"当判据 ✗ —— 错配同样会映射出更多行，
    结果把一份正常版式材料的勾稽从"差 184 万"弄成"差 172 亿"。
    """

    def test_split_layout_is_flagged(self) -> None:
        self.assertTrue(looks_like_split_rows(_split_page()))

    def test_normal_layout_is_not_flagged(self) -> None:
        self.assertFalse(looks_like_split_rows(_normal_page()),
                         "正常版式不该走坐标配对 —— 硬用会把本来对的行配错")

    def test_too_few_lines_is_not_judged(self) -> None:
        """行太少就不判（样本不足时宁可照旧）。"""
        self.assertFalse(looks_like_split_rows(_normal_page()[:8]))


if __name__ == "__main__":
    unittest.main()
