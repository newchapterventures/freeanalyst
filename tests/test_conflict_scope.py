"""同一科目多页取值时，取哪一页的？—— **按规模（资产总计）取，不按页序**。

## 为什么（这条是替换，不是新增）
原来写的是"**取首次出现**"，理由是"中文年报里合并表永远排在母公司表前面"。
那只是**惯例**：拿到排反的材料就不成立，而且它**不成文、不可核对**。
现在改用**该页的资产总计**当尺子（合并 = 母公司 + 子公司 − 抵销 → 合并那页通常更大）。

## 最重要的那条测试
`test_bigger_page_wins_even_when_it_comes_second` ——
**故意把规模大的那页放在后面**，看它还会不会选对。
只用"首次出现"的实现在这条上必然失败。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import canonical as cn                       # noqa: E402
from financials import statements as stm                     # noqa: E402
from financials.from_pdf import _resolve_conflicts           # noqa: E402


class _F:
    """可哈希的假字段（`SimpleNamespace` 不可哈希，当不了字典键）。"""

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return self.value


def _field() -> cn.Field:
    """用**真的** Field 当前景字段（类型正确，也少一层假东西）。"""
    return cn.Field.REVENUE


def _run(seen) -> stm.StatementSet:
    st = stm.StatementSet(name="利润表", source="测试")
    _resolve_conflicts(st, seen)
    return st


class TestConflictScope(unittest.TestCase):
    def test_bigger_page_wins_even_when_it_comes_second(self):
        """★ 关键：规模大的那页**排在后面**，仍要选它 —— 这是与"取首次出现"的分水岭。"""
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (10.0, 8, False)],        # 100 在 p7（先出现）
            cn.Field.TOTAL_ASSETS: [(500.0, 8, False), (50.0, 7, False)],  # p8 更大
        })
        self.assertEqual(st.fields[f], 10.0, "选了先出现的那个 —— 又回到页序约定了")
        self.assertIn("资产总计最大", st.conflicts[0])
        self.assertIn("第 8 页", st.conflicts[0])

    def test_first_page_wins_when_it_is_also_the_biggest(self):
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (10.0, 8, False)],
            cn.Field.TOTAL_ASSETS: [(500.0, 7, False), (50.0, 8, False)],
        })
        self.assertEqual(st.fields[f], 100.0)
        self.assertIn("与首次出现一致", st.conflicts[0])

    def test_no_scale_evidence_falls_back_and_says_so(self):
        """比不了规模 → 退回首次出现，但**必须明说没能比较**（不装作有依据）。"""
        f = _field()
        st = _run({f: [(100.0, 7, False), (10.0, 8, False)]})
        self.assertEqual(st.fields[f], 100.0)
        self.assertIn("未能按规模比较", st.conflicts[0])
        self.assertIn("请核对", st.conflicts[0])

    def test_one_page_only_is_not_enough_to_compare(self):
        """只有一页有资产总计 → 无从比较 → 同样明说。"""
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (10.0, 8, False)],
            cn.Field.TOTAL_ASSETS: [(500.0, 8, False)],
        })
        self.assertEqual(st.fields[f], 100.0)
        self.assertIn("未能按规模比较", st.conflicts[0])

    def test_equal_values_are_not_a_conflict(self):
        """同一个值出现两次不算冲突（**只看这个字段**，别的字段有自己的冲突）。"""
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (100.0, 8, False)],
            cn.Field.TOTAL_ASSETS: [(500.0, 8, False), (50.0, 7, False)],
        })
        self.assertEqual([c for c in st.conflicts if "营业收入" in c], [])
        # 等值时**不写字段**：没有冲突要裁决，字段由调用方（`_fill`）填 ——
        # 这里断言"不是这个函数负责的"，免得下次又写成必须写字段。
        self.assertNotIn(f, st.fields)

    def test_string_keys_are_recognised_too(self):
        """★ 实测踩到：`seen` 的键有时是**字符串**（某 A 股审计报告就是），
        原来只比对枚举 → 规模法完全没生效、悄悄退化成"未能按规模比较" ✗。"""
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (10.0, 8, False)],
            "资产总计": [(500.0, 8, False), (50.0, 7, False)],
        })
        self.assertEqual(st.fields[f], 10.0, "字符串键没被认出来 → 退回了首次出现")
        self.assertIn("资产总计最大", st.conflicts[0])

    def test_english_key_is_recognised(self):
        """美股/原生电子版那条路的键可能是英文。"""
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (10.0, 8, False)],
            "Total Assets": [(500.0, 8, False), (50.0, 7, False)],
        })
        self.assertEqual(st.fields[f], 10.0)

    def test_falls_back_to_another_scale_field(self):
        """★ 实测：某 A 股审计报告的**资产总计只抽到一页** → 比较不了。
        这时退到「负债合计」等（**同一字段内**比较，不跨字段混比），
        而且**提示里要说清用的是哪个字段**（说错字段等于编依据）。"""
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (10.0, 8, False)],
            "资产总计": [(500.0, 7, False)],                # 只有一页 → 不够比较
            "负债合计": [(400.0, 8, False), (40.0, 7, False)],
        })
        self.assertEqual(st.fields[f], 10.0, "退路没生效")
        self.assertIn("负债合计最大", st.conflicts[0])
        self.assertNotIn("资产总计最大", st.conflicts[0])

    def test_scale_fields_are_never_cross_compared(self):
        """一个字段只覆盖一页、另一个也只覆盖一页 → **不许**拿它们互相排大小。"""
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (10.0, 8, False)],
            "资产总计": [(500.0, 7, False)],
            cn.Field.TOTAL_LIABILITIES: [(40.0, 8, False)],
        })
        self.assertEqual(st.fields[f], 100.0, "跨字段比了大小 —— 这是没意义的比较")
        self.assertIn("未能按规模比较", st.conflicts[0])

    def test_other_values_are_never_dropped(self):
        """另一个值**不丢** —— 报出来，让人能核对。"""
        f = _field()
        st = _run({
            f: [(100.0, 7, False), (10.0, 8, False)],
            cn.Field.TOTAL_ASSETS: [(500.0, 8, False), (50.0, 7, False)],
        })
        self.assertIn("100.00", st.conflicts[0])
        self.assertIn("第 7 页", st.conflicts[0])


if __name__ == "__main__":
    unittest.main()
