"""可比公司数据源层（注册表 + 可插拔 + 诚实报缺）的测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasources import comps_source as cs  # noqa: E402


class _GoodSource:
    name = "good"

    def available(self):
        return True, "可用"

    def peers(self, criteria=None):
        return []


class _BadSource:
    name = "bad"

    def available(self):
        return False, "缺 MY_KEY"

    def peers(self, criteria=None):
        raise cs.SourceNotReady("bad 不可用：缺 MY_KEY")


class _BrokenSource:
    name = "broken"

    def available(self):
        raise RuntimeError("检查时炸了")

    def peers(self, criteria=None):
        return []


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self._saved = dict(cs.REGISTRY)
        cs.REGISTRY.clear()

    def tearDown(self):
        cs.REGISTRY.clear()
        cs.REGISTRY.update(self._saved)

    def test_register_and_get(self):
        cs.register(_GoodSource())
        self.assertIsNotNone(cs.get("good"))
        self.assertIsNone(cs.get("nope"))

    def test_duplicate_name_refused(self):
        """**同名默认拒绝覆盖。**

        静默替换会让人以为用的是自己接的那个源 —— 实际被别人顶掉了。
        """
        cs.register(_GoodSource())
        with self.assertRaises(ValueError) as cm:
            cs.register(_GoodSource())
        self.assertIn("已注册", str(cm.exception))

    def test_explicit_replace_allowed(self):
        cs.register(_GoodSource())
        cs.register(_GoodSource(), replace=True)   # 显式才让覆盖

    def test_list_reports_unavailable_with_reason(self):
        """**注册 ≠ 可用。** 不可用的要说清缺什么，不假装能跑。"""
        cs.register(_BadSource())
        rows = cs.list_sources()
        self.assertEqual(rows[0][0], "bad")
        self.assertFalse(rows[0][1])
        self.assertIn("MY_KEY", rows[0][2])

    def test_broken_available_does_not_crash(self):
        """`available()` 自己炸了也不能把清单整个打挂。"""
        cs.register(_BrokenSource())
        rows = cs.list_sources()
        self.assertFalse(rows[0][1])
        self.assertIn("RuntimeError", rows[0][2])

    def test_render_empty(self):
        self.assertIn("一个都没注册", cs.render_sources())


class TestPeerRow(unittest.TestCase):
    def test_missing_for_ev_ebitda(self):
        row = cs.PeerRow(name="x", enterprise_value=100.0)
        self.assertEqual(row.missing_for("EV/EBITDA"), ["ebitda"])

    def test_missing_for_pb(self):
        row = cs.PeerRow(name="x")
        self.assertEqual(row.missing_for("P/B"), ["market_cap", "equity"])

    def test_nothing_missing(self):
        row = cs.PeerRow(name="x", enterprise_value=1.0, ebitda=2.0)
        self.assertEqual(row.missing_for("EV/EBITDA"), [])

    def test_unknown_metric_lists_nothing(self):
        self.assertEqual(cs.PeerRow(name="x").missing_for("瞎写的"), [])


class TestSearchAPIHook(unittest.TestCase):
    """用户要「保留接入搜索 API 的接口」。"""

    def test_default_is_not_implemented(self):
        api = cs.SearchAPI()
        ok, why = api.available()
        self.assertFalse(ok)
        self.assertIn("未实现", why)

    def test_search_raises_until_implemented(self):
        with self.assertRaises(NotImplementedError):
            cs.SearchAPI().search("q", 10, consent=None)

    def test_doc_tells_you_the_three_rules(self):
        """接口文档必须写清三件事：查询词由谁生成、走哪个闸门、返回什么。"""
        doc = cs.SearchAPI.__doc__
        self.assertIn("build_queries", doc)
        self.assertIn("guarded_request", doc)
        self.assertIn("CloudConsent", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
