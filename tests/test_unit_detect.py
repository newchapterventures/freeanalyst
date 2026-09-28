"""金额单位判定 —— 认错就是 1000 倍级的**静默**错误，所以这里必须抠。

## 实测踩到的那个
某港股年报（人民币千元）正表页首标的是 `RMB’000`，
但正文别处还有一句 "in millions"（附注里另一段的口径）→
整份材料的单位被判成 **「百万美元」** ✗✗ —— **货币和量级两个都错**。

原因是判定顺序：笼统的英文短语排在**带货币的紧凑写法**前面，
先撞上谁算谁。正表上那个 `RMB’000` 才是该信的。

另外要守住：**认不出就返回空**，让调用方停下来问人，不许猜。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials.meta import detect_unit                    # noqa: E402


class TestDetectUnit(unittest.TestCase):
    def test_hk_compact_rmb_thousands(self):
        for text in ("RMB’000 RMB’000", "RMB'000", "RMB 000"):
            self.assertEqual(detect_unit(text)[0], "千元", text)

    def test_specific_beats_generic(self):
        """**这条就是实测踩到的 bug**：正文里有 "in millions"，但正表标的是 RMB'000。"""
        text = "Revenue 收入 RMB’000 RMB’000 ... (notes stated in millions)"
        self.assertEqual(detect_unit(text)[0], "千元")

    def test_english_chinese_wording_beats_generic(self):
        text = "人民币千元 2020年12月31日 ... in millions"
        self.assertEqual(detect_unit(text)[0], "千元")

    def test_us_filings_unchanged(self):
        self.assertEqual(detect_unit("(In thousands, except per share data)")[0], "千美元")
        self.assertEqual(detect_unit("(In millions)")[0], "百万美元")

    def test_hkd_and_usd_compact(self):
        self.assertEqual(detect_unit("HK$’000")[0], "千港元")
        self.assertEqual(detect_unit("US$'000")[0], "千美元")

    def test_cn_style(self):
        self.assertEqual(detect_unit("单位：人民币千元")[0], "千元")
        self.assertEqual(detect_unit("单位：万元")[0], "万元")

    def test_unknown_returns_empty_never_guesses(self):
        for text in ("", "no unit marker here at all"):
            self.assertEqual(detect_unit(text), ("", ""), text)


if __name__ == "__main__":
    unittest.main()
