"""来源分诊：材料是**原生电子版**还是**扫描件**，该怎么跟用户说。

## 为什么要有这层

实测某 192 dpi 审计报告扫描本：折旧摊销被读成小了三个量级，而形状完全合法
（任何形状校验都抓不到）。为救它试过三条路 —— 关语言纠正、逐格二次识别、
提高渲染倍率 —— **三条全部无效**，因为**分辨率就是上限**（详见 `bench/measure_ocr.py`）。

所以出路不是"再放大试试"，而是**换来源**：A 股/港交所的年报通常有原生带文字层的
电子版，根本不需要 OCR。这层分诊就是把这件事**在材料进来的时候**告诉用户，
而不是等他拿到一份带可疑标记的报告再去猜为什么。

## 最要紧的一条：不许误报

原生电子版不能被说成扫描件 —— 那会让用户怀疑一份本来完美的材料。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingest import pdf as ip                               # noqa: E402


class _FakeImage(dict):
    pass


class _FakePage:
    def __init__(self, width: float, images: list[dict], height: float = 842.0):
        self.width = width
        self.height = height
        self.images = images


class TestImageDpi(unittest.TestCase):
    def test_192dpi_scan_is_measured(self):
        # 实测那份材料：1587 px 宽 / 595 pt 页 → 192 dpi
        page = _FakePage(595.0, [_FakeImage(srcsize=(1587, 2245),
                                            width=595.0, height=842.0)])
        dpi = ip._image_dpi(page)
        self.assertEqual(len(dpi), 1)
        self.assertAlmostEqual(dpi[0], 1587 / (595 / 72), places=1)
        self.assertEqual(ip._median_int(dpi), 192)

    def test_logos_do_not_drag_the_number_down(self):
        """**实测踩到的坑**：一页 8 张图，整页扫描 1240 px（150 dpi），
        其余是页眉 logo/印章（占比 0%、7~63 dpi）。一起取中位数得到 20 dpi ✗ ——
        比真值小七倍，而且会直接印在给用户的提示里。
        """
        page = _FakePage(595.0, [
            _FakeImage(srcsize=(1240, 1754), width=595.0, height=842.0),   # 整页扫描
            _FakeImage(srcsize=(56, 52), width=13.0, height=12.0),         # logo
            _FakeImage(srcsize=(152, 44), width=36.0, height=11.0),        # 印章
            _FakeImage(srcsize=(88, 52), width=21.0, height=12.0),         # 签名
        ])
        self.assertEqual(ip._median_int(ip._image_dpi(page)), 150)

    def test_mostly_small_images_is_not_a_scan_page(self):
        """整页扫描的判据是**占比**，不是"有没有图"。"""
        page = _FakePage(595.0, [
            _FakeImage(srcsize=(56, 52), width=13.0, height=12.0),
            _FakeImage(srcsize=(152, 44), width=36.0, height=11.0),
        ])
        self.assertEqual(ip._image_dpi(page), [])

    def test_vector_page_has_no_dpi(self):
        self.assertEqual(ip._image_dpi(_FakePage(595.0, [])), [])
        self.assertEqual(ip._image_dpi(_FakePage(595.0, [_FakeImage(srcsize=(0, 0))])), [])

    def test_broken_page_does_not_raise(self):
        class Bad:
            width = 595.0

            @property
            def images(self):
                raise RuntimeError("boom")
        self.assertEqual(ip._image_dpi(Bad()), [])

    def test_median_int(self):
        self.assertEqual(ip._median_int([]), 0)
        self.assertEqual(ip._median_int([192.0, 300.0, 192.0]), 192)
        self.assertEqual(ip._median_int([100.0, 200.0]), 100)


class TestScanAdvisory(unittest.TestCase):
    def test_full_scan_gets_actionable_advice(self):
        adv = ip.scan_advisory(104, 104, 192)
        self.assertIn("扫描件", adv)
        self.assertIn("192", adv)
        # **必须给出路**，不能只报风险
        self.assertIn("原生电子版", adv)
        self.assertIn("人工复核", adv)

    def test_unknown_dpi_still_advises(self):
        adv = ip.scan_advisory(10, 10, 0)
        self.assertIn("扫描件", adv)
        self.assertIn("原生电子版", adv)

    def test_native_pdf_is_never_called_a_scan(self):
        """**不许误报** —— 原生电子版被说成扫描件，会让人怀疑一份本来完美的材料。"""
        self.assertEqual(ip.scan_advisory(176, 0, 0), "")
        self.assertEqual(ip.scan_advisory(100, 3, 192), "")
        self.assertEqual(ip.scan_advisory(0, 0, 0), "")


class TestIsScan(unittest.TestCase):
    def test_is_scan(self):
        pages = [ip.PdfPage(number=i, text="x") for i in (1, 2)]
        self.assertFalse(ip.PdfDocument(path=Path("a.pdf"), pages=pages).is_scan)
        self.assertTrue(ip.PdfDocument(path=Path("a.pdf"), pages=pages,
                                       ocr_pages=[1, 2]).is_scan)
        self.assertFalse(ip.PdfDocument(path=Path("a.pdf"), pages=[]).is_scan)


if __name__ == "__main__":
    unittest.main()
