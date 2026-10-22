"""现金流量表三个净额的映射。

## 真踩到的（某保险公司中报 601628）

现金流量表被解析成 122 行、**每一行出现两次**（表页范围重叠，同一张表拼了两遍），
值落到了段标题那一行：

    一、经营活动产生的现金流量        965,054   ← 值在这
    经营活动产生的现金流量净额         None      ← 真·净额行反而没值

于是「经营活动产生的现金流量」（不带「净额」）这条宽松写法**歪打正着**
把数字映射上了 —— 数字对、行错。

同一个毛病在另一份材料（中国平安）里方向相反：段标题没有值，真·净额行
写的是「经营活动**使用**的现金流量净额」（保险措辞），两样都不满足 → CFO 缺失。

★ 所以这份测试钉的是：

    ① **「使用」/「所用」都要认** —— 这是纯增益，实测修好了中国平安、零回归
    ② **宽松写法暂时必须留着** —— 删掉会净零（修好一份、弄坏另一份），
       真正该修的是上层的行错位，见 `canonical.py` 里那段注释
"""

from __future__ import annotations

import unittest

from financials.canonical import Field, identify


class TestNetAmountsMap(unittest.TestCase):
    """各种真·净额的写法，一个都不能丢。"""

    def test_standard_net(self):
        self.assertIs(identify("经营活动产生的现金流量净额")[0], Field.CFO)

    def test_short_net(self):
        self.assertIs(identify("经营活动现金流量净额")[0], Field.CFO)

    def test_h_share_used(self):
        """H 股写法：`所用`。"""
        self.assertIs(identify("经营活动所用现金净额")[0], Field.CFO)

    def test_insurance_uses_used(self):
        """★ 保险公司写法：`使用的`。实测就是这一处差异让 CFO 判不出来。"""
        self.assertIs(identify("经营活动使用的现金流量净额")[0], Field.CFO)

    def test_insurance_used_short(self):
        self.assertIs(identify("经营活动使用的现金净额")[0], Field.CFO)

    def test_english(self):
        self.assertIs(
            identify("Net cash used in operating activities")[0], Field.CFO)


class TestInvestigatingAndFinancing(unittest.TestCase):
    def test_cfi_net(self):
        self.assertIs(identify("投资活动产生的现金流量净额")[0], Field.CFI)

    def test_cfi_used(self):
        self.assertIs(identify("投资活动使用的现金流量净额")[0], Field.CFI)

    def test_cff_net(self):
        self.assertIs(identify("筹资活动产生的现金流量净额")[0], Field.CFF)

    def test_cff_used(self):
        self.assertIs(identify("筹资活动使用的现金流量净额")[0], Field.CFF)

    def test_cff_financing_variant(self):
        """港式写法用「融资活动」。"""
        self.assertIs(identify("融资活动产生的现金流量净额")[0], Field.CFF)


class TestKnownLimitation(unittest.TestCase):
    """**已知缺陷**：段标题也能匹配字段。

    这不是设计意图，是补偿 —— 有的材料把值落到了段标题那一行。
    钉住当前行为，是为了让「哪天把它删掉」这件事**必须是有意的**：

        删之前先修 `from_pdf.py` 的行错位，否则会修好一份、弄坏另一份。
    """

    def test_section_header_currently_matches_cfo(self):
        f, _ = identify("一、经营活动产生的现金流量")
        self.assertIs(f, Field.CFO,
                      "如果要改这里，先确认上层的行错位已经修好 —— "
                      "2026-10-21 直接删过一次，结果是净零（修好一份、弄坏一份）")

    def test_investing_header_currently_matches_cfi(self):
        self.assertIs(identify("二、投资活动产生的现金流量")[0], Field.CFI)

    def test_financing_header_currently_matches_cff(self):
        self.assertIs(identify("三、筹资活动产生的现金流量")[0], Field.CFF)


if __name__ == "__main__":
    unittest.main(verbosity=2)
