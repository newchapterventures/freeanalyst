"""可比公司选取流程的测试。

重点不是「选得准不准」（那是判断），是**几条纪律有没有守住**：
查询不泄密、样本不足拒绝给区间、基准日不许早于标的期间、指标不许代填。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valuation import comps_workflow as cw  # noqa: E402


class TestQueryPrivacy(unittest.TestCase):
    """**最重要的一组** —— 搜索在公网上跑，查询词就是泄密面。"""

    BLOCKED = ["某白酒公司", "某教育公司", "某上市公司"]

    def _sel(self, industries=("人力资源服务",)):
        s = cw.CompsSelection(as_of="2025-12-31", target_period="2025-12-31")
        s.criteria.industries = list(industries)
        s.criteria.geography = "中国"
        return s

    def test_query_has_no_target_name(self):
        qs = cw.build_queries(self._sel(), self.BLOCKED)
        self.assertTrue(qs)
        for q in qs:
            for n in self.BLOCKED:
                self.assertNotIn(n, q)

    def test_query_is_industry_only(self):
        qs = cw.build_queries(self._sel(), [])
        self.assertIn("人力资源服务", qs[0])
        self.assertIn("可比公司", qs[0])

    def test_blocked_name_in_criteria_raises(self):
        """**禁名混进筛选条件时必须中止，不能静默过滤。**

        静默过滤会让这个 bug 沉下去 —— 下次换个字段又漏一遍。
        要修的是上游（谁把标的名称传进了筛选条件），不是在这里偷偷把词删掉。
        """
        s = self._sel(industries=("某白酒公司",))
        with self.assertRaises(ValueError) as cm:
            cw.build_queries(s, self.BLOCKED)
        self.assertIn("禁名表", str(cm.exception))

    def test_one_char_names_dont_false_positive(self):
        """单字名不参与匹配 —— 否则「中」这种字会把所有查询都毙掉。"""
        qs = cw.build_queries(self._sel(), ["中", "国"])
        self.assertTrue(qs)

    def test_load_blocklist(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "block.txt"
            p.write_text("# 注释\n某公司\n\n某集团\n", encoding="utf-8")
            self.assertEqual(cw.load_blocklist(p), ["某公司", "某集团"])

    def test_missing_blocklist_is_empty(self):
        """没有禁名表就返回空 —— 但**空表等于没保护**，调用方要知道。"""
        self.assertEqual(cw.load_blocklist("/nonexistent/block.txt"), [])


class TestMinimumComps(unittest.TestCase):
    def _sel(self, n_accepted):
        s = cw.CompsSelection(as_of="2025-12-31",
                              criteria=cw.ScreeningCriteria(industries=["X"]),
                              metrics=["EV/EBITDA"])
        for i in range(3):
            s.candidates.append(cw.Candidate(
                name=f"公司{i}", accepted=(i < n_accepted)))
        return s

    def test_two_is_not_enough(self):
        """**2 家算出来的「中位数」没有意义。**"""
        s = self._sel(2)
        self.assertFalse(s.ready())
        gaps = " ".join(s.check())
        self.assertIn("不够 3 家", gaps)
        self.assertIn("拒绝给区间", gaps)

    def test_three_is_enough(self):
        s = self._sel(3)
        self.assertTrue(s.ready(), s.check())

    def test_rejected_candidates_dont_count(self):
        s = self._sel(3)
        s.candidates[0].accepted = False
        self.assertFalse(s.ready())


class TestDateAlignment(unittest.TestCase):
    def test_as_of_before_target_period_is_flagged(self):
        """**基准日早于标的期间要比出来。**

        拿 2024 年的可比公司倍数去比 2025 年的标的，倍数比标的还旧，
        比出来的数会系统性偏低。
        """
        s = cw.CompsSelection(
            as_of="2024-06-30", target_period="2025-12-31",
            criteria=cw.ScreeningCriteria(industries=["X"]), metrics=["EV/EBITDA"])
        s.candidates = [cw.Candidate(name=f"c{i}", accepted=True) for i in range(3)]
        self.assertTrue(any("早于标的报表期间" in g for g in s.check()), s.check())

    def test_missing_as_of_is_blocking(self):
        s = cw.CompsSelection(criteria=cw.ScreeningCriteria(industries=["X"]))
        self.assertIn("基准日没给", " ".join(s.check()))

    def test_aligned_is_ok(self):
        s = cw.CompsSelection(
            as_of="2025-12-31", target_period="2025-12-31",
            criteria=cw.ScreeningCriteria(industries=["X"]), metrics=["EV/EBITDA"])
        s.candidates = [cw.Candidate(name=f"c{i}", accepted=True) for i in range(3)]
        self.assertTrue(s.ready(), s.check())


class TestMetricsNotFilledIn(unittest.TestCase):
    def test_metrics_must_be_chosen(self):
        """**指标要用户确认，不许代填。**

        EV/EBITDA 还是 EV/收入是估值判断 —— 程序只列候选和各自的适用条件。
        """
        s = cw.CompsSelection(as_of="2025-12-31",
                              criteria=cw.ScreeningCriteria(industries=["X"]))
        s.candidates = [cw.Candidate(name=f"c{i}", accepted=True) for i in range(3)]
        self.assertIn("指标还没定", " ".join(s.check()))

    def test_metric_menu_gives_tradeoffs(self):
        """候选清单必须带「什么时候不适用」，否则等于没给判断依据。"""
        m = cw.metric_menu()
        self.assertIn("EV/EBITDA", m)
        self.assertIn("不适合", m)
        self.assertIn("EV/ARR", m)


class TestRender(unittest.TestCase):
    def test_worklist_lists_gaps(self):
        s = cw.CompsSelection()
        s.candidates.append(cw.Candidate(name="甲公司", why="同行业同地域"))
        text = cw.render_worklist(s)
        self.assertIn("甲公司", text)
        self.assertIn("还不能开始算", text)
        self.assertIn("行业还没定", text)

    def test_criteria_render(self):
        c = cw.ScreeningCriteria(industries=["人力资源服务"], geography="中国",
                                 size_min=1e8, size_max=1e9, business_model="to B")
        t = c.render()
        self.assertIn("人力资源服务", t)
        self.assertIn("中国", t)
        self.assertIn("to B", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
