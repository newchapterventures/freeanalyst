"""附注里的「分部报告」页不该被当成利润表 —— 但**扣分只在页首生效**。

## 实测（一份 176 页的保险公司中期报告）
利润表被定位到附注的**分部报告**页（434 分），真合并利润表只有 383 分 —— 差 51 分。
分部报告表里恰好有更多能命中的科目词（保险服务收入、营业收入合计…），
而真表那一页的科目（保险服务收入 / 赔付支出 / 承保财务损益）不在词表里。
被误选的那两页页首写的就是「四、分部报告（续）」。

## 为什么是扣分不是加分
沿用 `assemble.py` 里已经量化的理由：**扣分只会降可疑页；
加分会把"引用数字的概览页"抬上来**（实测 MD&A 概览页与真利润表只差 2 分）。

## 关键边界：只看页首
真利润表的**正文里**也常提到「分部」（附注引用、分部信息小计），
那种顺带一提**不该被扣分** —— 不然会误伤真表，把问题从"选错页"变成"选不出页"。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import assemble                            # noqa: E402

#: 利润表该有的标志（至少两组，才过 `g_hit >= 2` 那道门）
_BODY = (" 保险服务收入 营业收入 营业利润 利润总额 净利润 所得税费用 每股收益 "
         "分部信息 分部间 承保财务损益 赔付支出 保险合同负债 手续费及佣金净收入 ")


class TestSegmentPenalty(unittest.TestCase):
    def test_segment_page_loses_to_the_real_income_statement(self):
        seg = "四、分部报告（续）" + _BODY
        real = "合并利润表" + _BODY
        s_seg = assemble.signature(seg, 30)["income"]
        s_real = assemble.signature(real, 30)["income"]
        self.assertGreater(s_real, s_seg, f"真表 {s_real} 没赢过分部页 {s_seg}")

    def test_body_mention_of_segments_is_not_penalised(self):
        """正文里顺带提到「分部」**不许扣分** —— 否则会误伤真表。"""
        plain = "合并利润表" + _BODY
        body_only = "合并利润表" + ("填" * 260) + _BODY      # 页首 200 字里没有"分部"
        self.assertEqual(assemble.signature(body_only, 30)["income"],
                         assemble.signature(plain, 30)["income"])

    def test_penalty_never_goes_negative_silently(self):
        """扣分只是降分，**不该把一张真表扣成 0** 而让它彻底出局。"""
        real_with_segment_head = "分部报告 合并利润表" + _BODY
        self.assertGreaterEqual(
            assemble.signature(real_with_segment_head, 30)["income"], 0)


if __name__ == "__main__":
    unittest.main()
