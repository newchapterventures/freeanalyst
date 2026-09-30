"""「归属于母公司所有者权益合计」这类标签必须整串认出，不许退到宽泛候选。

## 真踩过（2026-09-30，某 A 股年报，公开来源）
表里写的是「归属于母公司所有者权益**合计**」，而词表里只有不带「合计」的写法 ✗
→ 整串匹配不上 → 退到「所有者权益合计」这个候选（那是**真的权益总额**）
→ 两行抢同一个字段、取到了归属于母公司那个数 → 勾稽差 **1,844,109.04**
（差额正好是少数股东权益）。

这个文件里早有同类记录（H 股那条：退到「权益总额」撞车、差一个少数股东权益 2,890）——
**同一类失效方式，换了个写法又踩一次**。
"""
from __future__ import annotations

import unittest

from financials import canonical as cn
from financials.canonical import Field


class TestEquityParentLabels(unittest.TestCase):
    def test_full_label_maps_to_equity_parent(self) -> None:
        for lab in ("归属于母公司所有者权益合计",
                    "归属于母公司股东权益合计",
                    "归属于母公司股东的权益合计",
                    "母公司所有者权益合计",
                    "归属于母公司所有者权益总计"):
            f, _ = cn.identify(lab)
            self.assertEqual(f, Field.EQUITY_PARENT,
                             f"「{lab}」应当映射到 EQUITY_PARENT，实际 {f}")

    def test_plain_total_still_maps_to_equity(self) -> None:
        """宽的「所有者权益合计」仍然是 EQUITY —— 两者必须分得开。"""
        for lab in ("所有者权益合计", "股东权益合计"):
            f, _ = cn.identify(lab)
            self.assertEqual(f, Field.EQUITY, f"「{lab}」应当映射到 EQUITY，实际 {f}")

    def test_the_two_are_not_the_same_field(self) -> None:
        """这条是问题的本体：两者**不能**落到同一个字段上。"""
        a, _ = cn.identify("归属于母公司所有者权益合计")
        b, _ = cn.identify("所有者权益合计")
        self.assertNotEqual(a, b, "归属于母公司与权益总额落到同一字段 → 必然撞车")


if __name__ == "__main__":
    unittest.main()
