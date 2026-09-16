"""「这条勾稽对这个格式适不适用」的测试。

## 为什么单独有一类

三种情况对用户的意义完全不同，**必须分开报**：

| 情况 | 报法 | 用户该做什么 |
|---|---|---|
| 材料缺了关键行 | **数据不足** | 去补材料 |
| 这种格式本来就没这条 | **不适用** | 不用管 |
| 差额不为零 | **不平** | 映射错了，别用结果 |

合并成一个「判不了」是最坏的做法。实测已经踩到三次：

1. **A 股现金流量表用直接法** —— 没有「净利润 → 经营现金流」那段调节
   （调节在附注里）。硬跑报「定位不到锚点」，看着像映射漏了。
2. **H 股现金流量表主表没有调节段** —— 只列一行「经营活动所用现金净额」
   并挂附注号（某 H 股公司是附注 22(b)），调节表在附注里。
3. **H 股资产负债表用 IFRS 净资产列报式** —— 根本没有「资产总计」
   「负债合计」行，`资产 = 负债 + 权益` 用不了。

前两次都曾被报成「数据不足」，让用户去翻一份本来就不该有这段的表。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import articulation as art              # noqa: E402
from financials import statements as stm                # noqa: E402
from financials.canonical import Field                  # noqa: E402


def _row(label, value, field=None):
    return stm.StatementRow(label=label, value=value, field=field, via="test")


def _income(fields=None):
    return stm.StatementSet(name="利润表", source="t", unit="千元",
                            fields=dict(fields or {Field.NET_INCOME: 1000.0}))


def _cash_flow(rows, fields=None):
    return stm.StatementSet(name="现金流量表", source="t", unit="千元",
                            rows=list(rows), fields=dict(fields or {}))


class TestIndirectMethodApplicability(unittest.TestCase):
    def _check(self, cf, inc=None):
        S = stm.Statements(cash_flow=cf, income=inc or _income(),
                           gaap="IFRS", scope="合并", audited="已审计",
                           period="2020-12-31")
        return next(c for c in S.checks() if "间接法" in c.render())

    def test_direct_method_is_not_applicable(self):
        """A 股：表里有「销售商品收到的现金」这些行，说明是直接法编的。"""
        cf = _cash_flow([
            _row("销售商品、提供劳务收到的现金", 100.0),
            _row("经营活动现金流入小计", 100.0),
            _row("经营活动产生的现金流量净额", 80.0, Field.CFO),
        ])
        r = self._check(cf)
        self.assertEqual(r.status, "不适用")
        self.assertIn("直接法", r.note)

    def test_no_reconciliation_section_is_not_applicable(self):
        """H 股：主表只列一行「经营活动所用现金净额」并挂附注号。

        调节表在**附注**里（实测某 H 股公司是附注 22(b)），主表本来就没有。
        报「数据不足」会让用户去翻一份不该有这段的表。
        """
        cf = _cash_flow([
            _row("Net cash used in operations 經營所用現金淨額 22(b)", -105575.0),
            _row("Income taxes paid 已付所得稅 26(a)", -2.0),
            _row("Net cash used in operating activities 經營活動所用現金淨額",
                 -105577.0, Field.CFO),
        ])
        r = self._check(cf)
        self.assertEqual(r.status, "不适用")
        self.assertIn("附注", r.note)

    def test_has_section_but_missing_anchor_is_missing_data(self):
        """**反过来：有调节段却找不到终点，那才是真的缺数据。**

        这里不能报「不适用」—— 段是有的，是映射没抓到终点。
        """
        cf = _cash_flow([
            _row("净利润", 1000.0, Field.NET_INCOME),
            _row("折旧及摊销", 200.0, Field.ID_DA),
            _row("经营活动产生的现金流量净额", None),      # 没映射上
        ])
        r = self._check(cf)
        self.assertEqual(r.status, "数据不足")
        self.assertIn(Field.CFO, r.missing)

    def test_reconciliation_runs_when_complete(self):
        """段齐了就真跑，而且要判得出来。"""
        cf = _cash_flow(
            [
                _row("净利润", 1000.0, Field.NET_INCOME),
                _row("折旧及摊销", 200.0, Field.ID_DA),
                _row("经营活动产生的现金流量净额", 1200.0, Field.CFO),
            ],
            # 真实流程里 `fields` 由这些行汇总而来，这里手工给上
            {Field.NET_INCOME: 1000.0, Field.CFO: 1200.0},
        )
        r = self._check(cf)
        self.assertEqual(r.status, "平")


class TestNotApplicableIsNotMissing(unittest.TestCase):
    """措辞本身也要锁住 —— 这两个词混用会让用户白忙。"""

    def test_status_words(self):
        self.assertEqual(art.Articulation("x", None, applicable=False).status,
                         "不适用")
        self.assertEqual(art.Articulation("x", None).status, "数据不足")
        self.assertNotEqual(
            art.Articulation("x", None, applicable=False).status,
            art.Articulation("x", None).status,
        )

    def test_report_counts_them_separately(self):
        """`cli.py` 的汇总也要分开数，否则报告会说「有 N 条判不了」。"""
        checks = [
            art.Articulation("a", True),
            art.Articulation("b", None, applicable=False, note="格式不同"),
            art.Articulation("c", None, missing=[Field.CFO]),
        ]
        skipped = [c for c in checks if not c.applicable]
        unknown = [c for c in checks if c.ok is None and c.applicable]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(len(unknown), 1)


if __name__ == "__main__":
    unittest.main()
