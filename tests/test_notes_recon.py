"""页面内勾稽：用报表**自己的恒等式**反查 OCR 有没有把数字读错。

## 为什么这条比形状校验硬

有一类错，形状校验永远抓不到：`1,234,567,890.12` 被读成 `1,234,567.12` ——
它依然是一个合法金额，只是小了三个量级。实测某 104 页扫描件的折旧摊销就是这么错的。

抓手是补充资料里的间接法恒等式：

    净利润 + Σ调节项（含折旧摊销）= 经营活动产生的现金流量净额

十几个数必须**同时对**才可能平；错一个就露馅，而且**不需要知道真值**。

## 两个必须守住的边界
  · 认不全（缺起点或终点）时**不许报警** —— 会喊狼来了的校验比没有校验更坏
  · OCR 常把负号认成汉字「一」（附注自己就写着「以“一”号填列」）——
    不处理会把所有负数当正数，恒等式必然不平，而那个"不平"是我们自己造成的
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import notes                               # noqa: E402


def _section(net_profit: float, cf0: float, da: float = 100.0,
             impair: float = 50.0) -> str:
    """造一段间接法调节段（数字是编的，结构与真材料一致）。"""
    def f(x: float) -> str:
        return f"{abs(x):,.2f}"
    minus = "一" if impair < 0 else ""
    return (
        "补充资料 本期数 上年同期数\n"
        f"净利润 {f(net_profit)} 0.00\n"
        f"加：资产减值准备 {minus}{f(impair)} 0.00\n"
        f"固定资产折旧、投资性房地产折旧 {f(da)} 0.00\n"
        f"无形资产摊销 {f(10.0)} 0.00\n"
        f"财务费用 0.00 0.00\n"
        f"经营活动产生的现金流量净额 {f(cf0)} 0.00\n"
    )


class TestReconcileIndirect(unittest.TestCase):
    def test_balanced_section_is_ok(self):
        # 净利润 100 + 减值 50 + 折旧 100 + 摊销 10 = 260
        r = notes.reconcile_indirect(_section(100.0, 260.0))
        self.assertTrue(r.checked, "该做核对却没做")
        self.assertTrue(r.ok, f"平的段被判不平（差 {r.diff}）")
        self.assertGreaterEqual(r.items, 3, "至少该认出减值/折旧/摊销三项")

    def test_one_wrong_digit_is_caught(self):
        """**这就是要抓的那类错**：一个数被读成小三个量级，形状仍合法。"""
        r = notes.reconcile_indirect(_section(100.0, 260.0, da=0.10))
        self.assertTrue(r.checked)
        self.assertFalse(r.ok, "数字被读错却没发现")

    def test_ocr_dash_variant_counts_as_negative(self):
        """OCR 把负号认成汉字「一」时，必须当负数 —— 否则不平是我们自己造成的。"""
        # 减值 -50：100 - 50 + 100 + 10 = 160
        r = notes.reconcile_indirect(_section(100.0, 160.0, impair=-50.0))
        self.assertTrue(r.ok, f"「一」号没被当成负号（差 {r.diff}）")

    def test_missing_anchor_never_alarms(self):
        """认不全就**不许报警** —— 缺起点或终点时说"没做"，而不是说"不平"。"""
        only_items = "固定资产折旧 100.00\n无形资产摊销 10.00\n"
        r = notes.reconcile_indirect(only_items)
        self.assertFalse(r.checked, "缺锚点却做了核对")
        self.assertFalse(r.ok)

    def test_amount_after_handles_forms(self):
        f = notes._amount_after
        self.assertEqual(f("净利润 1,234.50", "净利润"), 1234.50)
        self.assertEqual(f("财务费用 -1,234.50", "财务费用"), -1234.50)
        self.assertEqual(f("财务费用 一1,234.50", "财务费用"), -1234.50)
        self.assertEqual(f("财务费用 （1,234.50）", "财务费用"), 1234.50)
        self.assertIsNone(f("没有这一项 1.00", "财务费用"))


if __name__ == "__main__":
    unittest.main()
