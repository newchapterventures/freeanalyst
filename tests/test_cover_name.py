"""「标的名称」从哪来：封面文字，而不是文件名。

★ 判据是**量出来的**（2026-09-30 三份真实年报的第 1 页文字）：

    某 A 股年报   独立一行「某某某某数据科技集团股份有限公司」  → 读得到 ✓
    某 H 股年报   第 1 页是图片封面，文字层只有「二零二六年中报」  → 读不到 ✗
    某 A 股年报   第 1 页是 `(cid:…)` 乱码（字体没嵌 ToUnicode）   → 读不到 ✗

**读不到就返回空串** —— 这一条是核心：那道题问的是"报告封面上那个"，
文件名**不是**封面上那个。曾经默认值给的是文件名，于是报告标题印成了
「某公司2026年半年度报告」这种。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import intake                                     # noqa: E402


class CoverNameTest(unittest.TestCase):

    def test_a_normal_cover_yields_the_company(self):
        lines = [
            "某某某某数据科技集团股份有限公司2025年年度报告全文",
            "某某某某数据科技集团股份有限公司",
            "2025 年年度报告",
            "2026-011",
            "2026 年 4 月",
            "1",
        ]
        self.assertEqual(intake._pick_company_line(lines),
                         "某某某某数据科技集团股份有限公司")

    def test_the_document_title_alone_is_not_a_company(self):
        """「某某2026年半年度报告」—— 这是文件名那种串，不该被当成公司名。"""
        self.assertEqual(intake._pick_company_line(["某某2026年半年度报告"]), "")

    def test_an_image_cover_yields_nothing(self):
        self.assertEqual(intake._pick_company_line(["二零二六年中报"]), "")

    def test_cid_garbage_yields_nothing(self):
        lines = [": 601628", "(cid:34)(cid:32901)(cid:29296)(cid:10175)",
                 "2025", "年報"]
        self.assertEqual(intake._pick_company_line(lines), "")

    def test_a_name_with_the_title_glued_on_is_cut(self):
        self.assertEqual(
            intake._pick_company_line(["某某石油股份有限公司2025年年度报告"]),
            "某某石油股份有限公司")

    def test_a_bare_group_name_is_kept_as_a_weak_hit(self):
        self.assertEqual(intake._pick_company_line(["某某集团"]), "某某集团")

    def test_cover_name_never_falls_back_to_the_file_name(self):
        """真材料读不到封面名时，返回的必须是**空串**，不是文件名。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "某某2026年半年度报告.pdf"
            p.write_bytes(b"")            # 空文件：解析必然失败
            mat = intake.Materials(directory=p)
            self.assertTrue(mat.is_file)
            self.assertEqual(intake.cover_name(mat), "")

    def test_label_and_cover_name_are_different_things(self):
        """`label` 是材料名，`cover_name` 是标的名的候选 —— 两者不能混用。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "某某2026年半年度报告.pdf"
            p.write_bytes(b"")
            mat = intake.Materials(directory=p)
            self.assertEqual(mat.label, "某某2026年半年度报告")
            self.assertNotEqual(intake.cover_name(mat), mat.label)


if __name__ == "__main__":
    unittest.main()
