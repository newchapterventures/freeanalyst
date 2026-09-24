"""装模型这条路：官方 registry 查询 + 服务端拉取。

盯住四件事：

1. **不带按次授权，绝不联网** —— registry.ollama.ai 是外部主机，`guard` 默认拦。
   没有授权就回退本地清单，而且**必须如实说"这是回退"**，不许把回退说成"最新"。
2. **查不到就说查不到**（离线、被拦、官方改版），不编体积数字。
3. **模型名先校验再发**，别把奇怪的东西拼进请求。
4. 一次只跑一个下载（两个同时拉会把内存和带宽一起打满，用户还不知道发生了什么）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webapp                                    # noqa: E402
from llm import registry                        # noqa: E402


class TestTiers(unittest.TestCase):

    def test_tier_by_ram(self):
        self.assertEqual(registry.tier_for(8), "8")
        self.assertEqual(registry.tier_for(16), "16")
        self.assertEqual(registry.tier_for(24), "24")
        self.assertEqual(registry.tier_for(64), "32")

    def test_unknown_ram_falls_to_the_smallest(self):
        """内存探不到 → 给最小的档（宁小不大：给大了会把机器拖垮）。"""
        self.assertEqual(registry.tier_for(0), "8")

    def test_candidates_are_current_generation_first(self):
        """候选清单里，**新一代排在旧一代前面** —— 这是"清单会过期"的解药。"""
        for tier, cands in registry.CANDIDATES.items():
            self.assertTrue(cands, tier)
            self.assertTrue(cands[0].startswith("qwen3.5") or cands[0].startswith("qwen3.6"),
                            f"{tier} 档当前代没排在最前面：{cands[0]}")


class TestNoNetworkWithoutConsent(unittest.TestCase):

    def test_check_without_consent_stays_offline(self):
        r = registry.check_candidates(16, consent=None, refresh=True)
        self.assertEqual(r["source"], "offline",
                         "没有授权却报出 registry/cache = 偷偷联网了")
        self.assertIn("本地清单", r["why"])
        self.assertTrue(r["picked"], "回退也要给出一个可用的候选")

    def test_manifest_query_without_consent_returns_nothing(self):
        """被 guard 拦下 → 返回 None，而不是抛异常或猜一个数。"""
        self.assertIsNone(registry.manifest_size_gb("qwen3.5", "4b", timeout=3,
                                                    consent=None))

    def test_api_model_check_defaults_to_offline(self):
        r = webapp.api_model_check(16.0, consent_ok=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "offline")


class TestPullValidation(unittest.TestCase):

    def test_empty_model_rejected(self):
        r = webapp.api_pull_start("   ")
        self.assertFalse(r["ok"])
        self.assertIn("模型名", r["error"])

    def test_weird_characters_rejected(self):
        """字符合法但形状荒唐的也要挡住（`../../etc/passwd` 曾从白名单漏过去）。"""
        for bad in ("qwen3.5:9b; rm -rf /", "模型名", "../../etc/passwd",
                    "a" * 200, ".hidden", "-leading-dash"):
            r = webapp.api_pull_start(bad)
            self.assertFalse(r["ok"], bad)
            self.assertIn("模型名", r["error"])

    def test_only_one_pull_at_a_time(self):
        webapp._PULLS["current"] = {"model": "qwen3.5:9b", "running": True}
        try:
            r = webapp.api_pull_start("qwen3.5:4b")
            self.assertFalse(r["ok"])
            self.assertIn("已经在拉", r["error"])
        finally:
            webapp._PULLS.pop("current", None)

    def test_status_is_safe_to_poll_when_idle(self):
        webapp._PULLS.pop("current", None)
        s = webapp.api_pull_status()
        self.assertFalse(s.get("running"))


class TestRegistryPageContract(unittest.TestCase):
    """页面要用的字段/函数一个都不能少 —— 少了那段界面就静默空着。"""

    def test_page_has_the_install_block(self):
        html = webapp.config_html()
        for token in ("install", "pullModel", "checkLatest", "/api/pull",
                      "/api/model-check", "联网查最新"):
            self.assertIn(token, html, token)

    def test_page_says_what_is_sent_before_asking(self):
        """联网确认框里必须写明 主机 / 用途 / 发什么 —— 授权要"知情"。"""
        html = webapp.config_html()
        self.assertIn("registry.ollama.ai", html)
        self.assertIn("不发送", html)
        self.assertIn("审计", html)

    def test_service_advertises_the_endpoints(self):
        for ep in ("pull", "pull-status", "model-check"):
            self.assertIn(ep, webapp.ENDPOINTS)
        self.assertIn("install-model", webapp.FEATURES)


if __name__ == "__main__":
    unittest.main()
