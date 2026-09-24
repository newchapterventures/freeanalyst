"""logo 资产：必须和网页页头**同源**，且真的透明可商用。

盯住四件事：

1. **尺寸/颜色/字距不许和网页走散** —— logo 是照着 `webapp_page.html` 的页头复刻的
   （算盘 48px 高 → 宽 = 48×19/33、字标 33px/字距 .24em、署名 11px/.14em、间距 16px、
   三色 #E9B949 / #C9D2DC / #E5484D）。改了网页没改这里，导出图就和产品对不上 —— 测试挡住。
2. **PNG 必须带 alpha**（IHDR 颜色类型 6 = RGBA）。这个不用图库，读文件头就能判。
3. **文件名里的倍数要等于实际像素**（@2x 就得是 720×120），否则用的人会按错尺寸摆。
4. 算盘必须还是 **28 个方块**（4 列 × 上 2 下 5 中的 3+1+20 … 合计 28），
   红块一颗，位置是"左起第 1 列 · 上排第 2 颗"。
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BRAND = ROOT / "brand"
HTML = BRAND / "logo-full.html"
PAGE = ROOT / "webapp_page.html"

#: 逻辑画布 360×60（见 logo-full.html），下面这些是各倍数的实际像素
SCALES = {"logo-full.png": (2, 720, 120), "logo-full@1x.png": (1, 360, 60),
          "logo-full@2x.png": (2, 720, 120), "logo-full@4x.png": (4, 1440, 240),
          "logo-full@8x.png": (8, 2880, 480)}


def png_header(path: Path) -> tuple[int, int, int]:
    """读 PNG 头：宽 / 高 / 颜色类型（6 = RGBA，带 alpha）。"""
    b = path.read_bytes()[:33]
    assert b[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    w, h = struct.unpack(">II", b[16:24])
    return w, h, b[25]


class TestBrandFiles(unittest.TestCase):

    def test_renderer_and_images_exist(self):
        self.assertTrue(HTML.exists(), "缺渲染页 brand/logo-full.html")
        for name in SCALES:
            self.assertTrue((BRAND / name).exists(), f"缺 {name}")

    def test_pngs_really_have_transparency(self):
        """IHDR 颜色类型 6 = 真彩 + alpha。没有 alpha 的图，放上去就是一块底。"""
        for name in SCALES:
            w, h, ctype = png_header(BRAND / name)
            self.assertEqual(ctype, 6, f"{name} 没有 alpha 通道（颜色类型 {ctype}）")

    def test_pixel_size_matches_the_scale_in_the_name(self):
        for name, (_scale, w, h) in SCALES.items():
            got = png_header(BRAND / name)[:2]
            self.assertEqual(got, (w, h), f"{name} 实际 {got}，文件名说是 {(w, h)}")


class TestBrandMatchesThePage(unittest.TestCase):
    """logo 与网页页头同一套规格 —— 走散了就是两张不同的脸。"""

    @classmethod
    def setUpClass(cls):
        cls.brand = HTML.read_text(encoding="utf-8")
        cls.page = PAGE.read_text(encoding="utf-8")

    def test_same_colours(self):
        for colour in ("#E9B949", "#C9D2DC", "#E5484D", "#D7F5E9", "#00E5A0",
                       "#42565A"):
            self.assertIn(colour, self.brand, colour)
            self.assertIn(colour, self.page, colour)

    def test_same_typography(self):
        # 网页的字号挂在变量上（--word-size:33px），logo 里是直接写的 33px ——
        # 值必须一致，写法允许不同（网页那个是为了能当旋钮调）。
        for token in ("letter-spacing:.24em", "letter-spacing:.14em",
                      "font-size:11px", "font-weight:700"):
            self.assertIn(token, self.brand, token)
            self.assertIn(token, self.page, token)
        self.assertIn("font-size:33px", self.brand)
        self.assertIn("--word-size:33px", self.page)

    def test_same_gap_and_height(self):
        self.assertIn("gap:16px", self.brand)
        self.assertIn("height:48px", self.brand)
        # 宽度由高度算出来，两边都不许写死一个数
        self.assertIn("calc(48px * 19 / 33)", self.brand)
        self.assertIn("calc(var(--logo-h) * 19 / 33)", self.page)

    def test_abacus_is_still_28_blocks(self):
        self.assertEqual(self.brand.count("<rect "), self.page.count("<rect "),
                         "logo 里的方块数和网页页头不一致")
        blocks = self.brand.split("<g class=\"lg-up\">")[1].split("</g>")[0].count("<rect ")
        down = self.brand.split("<g class=\"lg-down\">")[1].split("</g>")[0].count("<rect ")
        self.assertEqual(blocks + down + 1, 28, "算盘不是 28 个方块了")
        self.assertIn('<rect class="lg-acc" x="2" y="6"', self.brand,
                      "红块位置变了（应在左起第 1 列 · 上排第 2 颗）")

    def test_two_text_lines(self):
        self.assertIn("FREE<em>ANALYST</em>", self.brand)
        self.assertIn("by New Chapter Ventures", self.brand)


if __name__ == "__main__":
    unittest.main()
