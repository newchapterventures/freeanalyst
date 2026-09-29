"""单位识别的写法覆盖 —— 实测踩到的那两种写法必须认得出。

背景（2026-09-29 实测）：中国人寿与中国平安两份年报，
正表页首写的是 **「(除特别注明外,金额单位为人民币百万元)」**，
而引擎判成了「元」—— 差 **100 万倍**，数字形状完全正常，肉眼看不出来。

两个原因，各有一个测试钉着：
  ① 写法：`单位：…` 要求冒号，而材料用的是 **「单位为…」** → 认不出
  ② 范围：整份文档里 search 是"先撞上谁算谁"，而正表在第 89 页、前面几十页
     到处都有「单位：元」 → 挑到了错的那个（所以调用方**必须只喂正表页**）
"""
from __future__ import annotations

import unittest

from financials import meta


class TestUnitPhrasings(unittest.TestCase):
    def test_accepts_the_wei_phrasing(self):
        """★ 「金额单位为人民币百万元」—— 没有冒号，用「为」。"""
        for text in ("(除特别注明外,金额单位为人民币百万元)",
                     "金额单位：人民币百万元",
                     "单位:人民币百万元",
                     "单位 为 人民币百万元"):
            with self.subTest(text=text):
                unit, basis = meta.detect_unit(text)
                self.assertEqual(unit, "百万元", f"没认出：{text}")
                self.assertTrue(basis, "依据也要给出来")

    def test_does_not_let_yuan_steal_the_million(self):
        """「百万元」不能被「元」抢走 —— 正则从左到右试，位置不同结果不同。"""
        self.assertEqual(meta.detect_unit("金额单位为人民币百万元")[0], "百万元")
        self.assertEqual(meta.detect_unit("单位：人民币千元")[0], "千元")
        self.assertEqual(meta.detect_unit("单位：元")[0], "元")

    def test_says_nothing_when_the_material_says_nothing(self):
        """认不出就返回空 —— 这层的纪律是**不猜**（默认给「元」是 1000 倍级的静默错）。"""
        self.assertEqual(meta.detect_unit("本公司无此项目")[0], "")
        self.assertEqual(meta.detect_unit("")[0], "")

    def test_whole_document_search_would_pick_the_wrong_one(self):
        """★ 记录**为什么**调用方必须只喂正表页。

        下面这段是真实材料的形态：附注里先出现「单位：元」，正表在后面写「百万元」。
        在整份文档上 search → 拿到的是「元」✗（这就是 bug 的形态）。
        喂正表页那一小段 → 拿到「百万元」✓。
        """
        whole = "会计政策…各项金额单位：元（另有说明除外）…" * 50 + \
                "合并资产负债表 (除特别注明外,金额单位为人民币百万元) 货币资金 50,879"
        self.assertEqual(meta.detect_unit(whole)[0], "元",
                         "整份文档上 search 会挑到错的 —— 这正是要避免的用法")
        statement_pages_only = "合并资产负债表 (除特别注明外,金额单位为人民币百万元) 货币资金 50,879"
        self.assertEqual(meta.detect_unit(statement_pages_only)[0], "百万元")


if __name__ == "__main__":
    unittest.main()
