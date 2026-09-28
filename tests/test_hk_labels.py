"""港股繁体年报的**亏损形态**写法 —— 实测踩到的空缺。

## 起因
某港交所年报（繁体 + 双语）的利润表里，经营利润那一行写的是：

    Loss from operations  經營虧損

之前只补过「經營**溢利**」这一种写法，**亏损年份整行认不出来** →
EBITDA 直接报"数据不足"（说材料里没有营业利润行，其实是写法没覆盖）。
用繁体年报的公司处在亏损期非常常见，这条线必须堵上。

**只认带限定的形态**：不往表里加光秃秃的「虧損」——
那样「除稅前虧損」「年度虧損」会互相抢，映射就串了。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials.canonical import Field, identify            # noqa: E402


class TestTraditionalLossForms(unittest.TestCase):
    def test_operating_income_loss_forms(self):
        """实测那行：`Loss from operations 經營虧損`。"""
        for label in ("經營虧損", "營業虧損", "经营亏损", "营业亏损",
                      "經營溢利", "營業溢利", "經營虧損淨額",
                      "Loss from operations", "Operating loss"):
            f, via = identify(label)
            self.assertEqual(f, Field.OPERATING_INCOME, f"{label} → {f}（依据 {via}）")

    def test_bilingual_hk_row_is_recognised(self):
        f, via = identify("Loss from operations 經營虧損")
        self.assertEqual(f, Field.OPERATING_INCOME, f"整行认不出（依据 {via}）")

    def test_pretax_loss_forms(self):
        for label in ("除稅前虧損", "除税前亏损", "稅前虧損", "除稅前溢利",
                      "Loss before taxation", "Profit before tax"):
            f, _ = identify(label)
            self.assertEqual(f, Field.PRETAX_INCOME, f"{label} → {f}")

    def test_net_income_loss_forms(self):
        for label in ("年度虧損", "年度亏损", "年內虧損", "年度溢利",
                      "Loss for the year", "Profit for the year"):
            f, _ = identify(label)
            self.assertEqual(f, Field.NET_INCOME, f"{label} → {f}")

    def test_three_forms_do_not_cross(self):
        """三种形态**不许互相串** —— 串了就是静默的科目错位。"""
        self.assertEqual(identify("經營虧損")[0], Field.OPERATING_INCOME)
        self.assertEqual(identify("除稅前虧損")[0], Field.PRETAX_INCOME)
        self.assertEqual(identify("年度虧損")[0], Field.NET_INCOME)


if __name__ == "__main__":
    unittest.main()
