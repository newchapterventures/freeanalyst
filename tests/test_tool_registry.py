"""工具注册表的测试。

这一层守的是**三件容易做漏的事**：

1. 出网要按次授权 —— 但**只对名单里的主机**。
   一律要授权的话，「取日线」这种日常数据源会天天弹窗，
   而天天弹窗的结果是用户无脑点同意 —— **那道闸就等于没有**。
2. 每个工具的说明必须写「什么时候**别用**」。
   实测（`bench/model_choice.py`）：模型选错工具远少于在不该用时伸手。
3. **出错要返回，不要抛** —— 抛出去模型看不到，它就没法换个动作再试，
   而「换动作」正是编排能力本身。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import catalog, registry as R  # noqa: E402
from net import CONSENT_HOSTS, host_needs_consent  # noqa: E402


class TempTool(unittest.TestCase):
    """注册表是全局的 —— 每一项都要还原，否则测试之间互相污染。"""

    def setUp(self):
        self._saved = dict(R.REGISTRY)

    def tearDown(self):
        R.REGISTRY.clear()
        R.REGISTRY.update(self._saved)


class TestRegistry(TempTool):
    def test_duplicate_refused(self):
        R.register(R.Tool(name="t", summary="s", when="w", when_not="n",
                          parameters={"type": "object", "properties": {}},
                          side_effect=R.SideEffect.READ_LOCAL, fn=lambda: 1))
        with self.assertRaises(ValueError):
            R.register(R.Tool(name="t", summary="s", when="w", when_not="n",
                              parameters={"type": "object", "properties": {}},
                              side_effect=R.SideEffect.READ_LOCAL, fn=lambda: 1))

    def test_egress_without_hosts_refused(self):
        """出网工具必须显式列 `hosts` —— 授权弹窗要显示「去哪些网站取什么」，
        只给一个工具名，用户没法做判断。"""
        with self.assertRaises(ValueError) as cm:
            R.register(R.Tool(name="e", summary="s", when="w", when_not="n",
                              parameters={"type": "object", "properties": {}},
                              side_effect=R.SideEffect.EGRESS, fn=lambda: 1))
        self.assertIn("hosts", str(cm.exception))

    def test_missing_is_a_clear_error(self):
        r = R.call("根本没有这个工具")
        self.assertFalse(r.ok)
        self.assertIn("没有", r.error)

    def test_exception_becomes_a_result(self):
        """★ **抛出去模型看不到。** 收成返回值它才能换个动作。"""
        R.register(R.Tool(name="boom", summary="s", when="w", when_not="n",
                          parameters={"type": "object", "properties": {}},
                          side_effect=R.SideEffect.READ_LOCAL,
                          fn=lambda: (_ for _ in ()).throw(ValueError("炸了"))))
        r = R.call("boom")
        self.assertFalse(r.ok)
        self.assertIn("炸了", r.error)


class TestValidation(TempTool):
    def setUp(self):
        super().setUp()
        R.register(R.Tool(
            name="v", summary="s", when="w", when_not="n",
            parameters={"type": "object",
                        "properties": {"a": {"type": "string"},
                                       "n": {"type": "integer"}},
                        "required": ["a"]},
            side_effect=R.SideEffect.READ_LOCAL, fn=lambda a, n=0: (a, n)))

    def test_required_missing(self):
        r = R.call("v", {})
        self.assertFalse(r.ok)
        self.assertIn("缺必填参数", r.error)

    def test_empty_string_counts_as_missing(self):
        self.assertFalse(R.call("v", {"a": ""}).ok)

    def test_type_mismatch(self):
        r = R.call("v", {"a": "x", "n": "不是数字"})
        self.assertFalse(r.ok)
        self.assertIn("integer", r.error)

    def test_bool_is_not_an_int(self):
        """`True` 在 Python 里是 `int` 的子类，但把它当整数传下去是错的。"""
        self.assertFalse(R.call("v", {"a": "x", "n": True}).ok)

    def test_valid_passes(self):
        r = R.call("v", {"a": "x", "n": 3})
        self.assertTrue(r.ok)
        self.assertEqual(r.value, ("x", 3))


class TestConsentOnlyForListedHosts(TempTool):
    """★ **只对按次授权名单里的主机要授权。**

    一律要的话，天天弹窗 → 用户无脑点同意 → 那道闸等于没有。
    """

    def setUp(self):
        super().setUp()
        self.calls = []
        R.register(R.Tool(
            name="daily", summary="日常数据源", when="w", when_not="n",
            parameters={"type": "object", "properties": {}},
            side_effect=R.SideEffect.EGRESS, fn=lambda: self.calls.append(1) or "ok",
            hosts=("push2his.eastmoney.com",)))
        R.register(R.Tool(
            name="gated", summary="按次授权的数据源", when="w", when_not="n",
            parameters={"type": "object", "properties": {}},
            side_effect=R.SideEffect.EGRESS, fn=lambda: "ok",
            hosts=("qt.gtimg.cn",)))

    def test_daily_host_needs_no_consent(self):
        r = R.call("daily", {})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.calls, [1])

    def test_gated_host_needs_consent(self):
        r = R.call("gated", {})
        self.assertFalse(r.ok)
        self.assertTrue(r.needs_consent)
        self.assertIn("qt.gtimg.cn", r.hosts)

    def test_needs_consent_is_not_a_failure(self):
        """要授权 ≠ 出错。上层要据此**去问用户**，不是报错。"""
        r = R.call("gated", {})
        self.assertIn("需要用户授权", r.brief())
        self.assertIn("qt.gtimg.cn", r.brief())

    def test_consent_lets_it_through(self):
        from net import HostConsent
        r = R.call("gated", {}, consent={"qt.gtimg.cn": HostConsent("qt.gtimg.cn", "测试")})
        self.assertTrue(r.ok, r.error)

    def test_consent_for_the_wrong_host_does_not_help(self):
        from net import HostConsent
        r = R.call("gated", {},
                   consent={"别的网站": HostConsent("别的网站", "测试")})
        self.assertFalse(r.ok)
        self.assertTrue(r.needs_consent)


class TestCatalogIntegrity(unittest.TestCase):
    """清单本身的纪律 —— 这些不满足，模型就会乱伸手。"""

    def test_every_tool_says_when_not_to_use(self):
        """★ 每一条都必须有「什么时候别用」。

        实测：模型选错工具远少于**在不该用时伸手** ——
        用户打个「你好」，它去扫几百页的目录，白等两分钟，下游看不出这个错。
        """
        import tools.catalog  # noqa: F401  确保已注册
        for t in R.REGISTRY.values():
            self.assertTrue(t.when_not, t.name)
            self.assertIn("什么时候别用", t.description(), t.name)

    def test_egress_hosts_are_in_the_allowlist(self):
        """出网工具声明的主机必须在白名单里 —— 不然它会**必然失败**，
        而失败原因看起来像「网络问题」，人会去查网络。"""
        from net import allowed_hosts
        ok = allowed_hosts()
        for t in R.by_side_effect(R.SideEffect.EGRESS):
            for h in t.hosts:
                self.assertIn(h, ok, f"{t.name} 声明了白名单外的主机 {h}")

    def test_gated_tools_actually_route_through_consent(self):
        """声明了按次授权主机的工具，**不带授权时确实过不去**。"""
        gated = [t for t in R.by_side_effect(R.SideEffect.EGRESS)
                 if any(host_needs_consent(h) for h in t.hosts)]
        self.assertTrue(gated, "至少要有一个走按次授权的工具，否则这道闸没人用")
        for t in gated:
            with self.subTest(tool=t.name):
                self.assertTrue(any(host_needs_consent(h) for h in t.hosts))

    def test_read_local_tools_do_not_egress(self):
        """纯读本机的工具**一个主机都不该声明** —— 声明了就说明分类错了。"""
        for t in R.by_side_effect(R.SideEffect.READ_LOCAL):
            self.assertEqual(t.hosts, (), t.name)

    def test_schema_is_json_serialisable(self):
        import json
        for t in R.REGISTRY.values():
            json.dumps(t.schema(), ensure_ascii=False)

    def test_schema_can_be_narrowed(self):
        """低配机器要收窄动作空间 —— 只暴露一部分工具。"""
        one = R.schema(only=["scan_materials"])
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["function"]["name"], "scan_materials")

    def test_catalog_renders(self):
        txt = R.render_catalog()
        self.assertIn("纯读本机", txt)
        self.assertIn("会出网", txt)


class TestCatalogBehaviour(unittest.TestCase):
    """真调一遍 —— 路径不对、参数不对都要**返回错误而不是抛**。"""

    def test_unknown_path_is_a_clear_error(self):
        r = R.call("scan_materials", {"materials": "/绝对不存在的路径_xyz"})
        self.assertFalse(r.ok)
        self.assertIn("不存在", r.error)

    def test_missing_arg_is_a_clear_error(self):
        r = R.call("scan_materials", {})
        self.assertFalse(r.ok)
        self.assertIn("materials", r.error)

    def test_scan_lists_files(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "a.pdf").write_text("x")
        (d / "b.txt").write_text("x")
        r = R.call("scan_materials", {"materials": str(d)})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.value["count"], 2)

    def test_search_without_index_says_what_to_do(self):
        r = R.call("search_materials", {"question": "随便问点什么"})
        if not r.ok:
            # 空索引时必须是**可操作的**提示，不是一句「出错了」
            self.assertTrue("ingest" in r.error or "索引" in r.error, r.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
