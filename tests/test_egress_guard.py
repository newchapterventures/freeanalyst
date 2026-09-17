"""出网闸门的自测 —— 这是这个项目最重要的测试。

它证明"数据零出境"不是口号，是可验证的事实。

设计原则：如果这个测试失败，项目就不该发布。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 让审计日志写到临时目录，避免污染真实日志
_TMP = tempfile.mkdtemp(prefix="freeanalyst-test-")
os.environ["FREEANALYST_AUDIT"] = str(Path(_TMP) / "egress.jsonl")

import guard  # noqa: E402


class TestEgressGuard(unittest.TestCase):
    def setUp(self) -> None:
        guard.AUDIT_PATH = Path(os.environ["FREEANALYST_AUDIT"])
        if guard.AUDIT_PATH.exists():
            guard.AUDIT_PATH.unlink()

    def test_blocks_cloud_llm_providers(self) -> None:
        """所有主流大模型厂商的地址必须被拦下。"""
        urls = [
            "https://api.openai.com/v1/chat/completions",
            "https://api.anthropic.com/v1/messages",
            "https://generativelanguage.googleapis.com/v1/models",
            "https://api.deepseek.com/chat/completions",
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(guard.EgressBlocked):
                    guard.guarded_request(url, purpose="self-test")

    def test_blocks_arbitrary_hosts(self) -> None:
        """任何非回环地址都要拦住，包括内网 IP 和各类协议。"""
        urls = [
            "http://example.com/",
            "https://1.1.1.1/dns-query",
            "http://192.168.1.1/admin",
            "ftp://files.example.org/x",
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(guard.EgressBlocked):
                    guard.guarded_request(url, purpose="self-test")

    def test_allows_loopback_only(self) -> None:
        """回环地址必须在白名单里（本地模型跑在这里）。"""
        for url in (
            "http://127.0.0.1:11434/api/tags",
            "http://localhost:11434/api/tags",
        ):
            with self.subTest(url=url):
                host = url.split("/")[2].split(":")[0]
                self.assertIn(host, guard.ALLOWED_HOSTS)

    def test_every_attempt_is_logged(self) -> None:
        """放行和拦截都必须留痕，一条不漏。"""
        for url in ("https://api.openai.com/v1/x", "http://localhost:1/x"):
            try:
                guard.guarded_request(url, purpose="self-test", timeout=1)
            except Exception:  # noqa: BLE001
                pass

        records = [
            json.loads(line)
            for line in guard.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(records), 2, "每次出网尝试都应有一条审计记录")
        self.assertEqual(records[0]["decision"], "BLOCK")
        self.assertEqual(records[0]["host"], "api.openai.com")
        self.assertEqual(records[1]["decision"], "ALLOW")

    def test_summary_reports_external_hosts(self) -> None:
        """审计汇总必须能报出被拦截的外部主机。"""
        try:
            guard.guarded_request("https://api.anthropic.com/v1/messages", purpose="self-test")
        except Exception:  # noqa: BLE001
            pass

        summary = guard.audit_summary()
        self.assertEqual(summary["blocked"], 1)
        self.assertIn("api.anthropic.com", summary["external_hosts"])


class TestCloudConsent(unittest.TestCase):
    """云端模型的按次授权。

    用户的原话：「有些信息用户觉得没必要保密，所以可以让闭源云端大模型来分析。」

    所以**不是**放开云端，而是**默认拦、按次授权才放** —— 而且授权这件事本身
    也要留痕。这里测的就是这条边界有没有守住。
    """

    def setUp(self) -> None:
        guard.AUDIT_PATH = Path(os.environ["FREEANALYST_AUDIT"])
        if guard.AUDIT_PATH.exists():
            guard.AUDIT_PATH.unlink()

    def _records(self) -> list[dict]:
        if not guard.AUDIT_PATH.exists():
            return []
        return [json.loads(l) for l in guard.AUDIT_PATH.read_text().splitlines() if l.strip()]

    def test_still_blocked_without_consent(self) -> None:
        """没有授权 -> 照样拦。**这是默认状态，不能变。**"""
        with self.assertRaises(guard.EgressBlocked):
            guard.guarded_request("https://api.deepseek.com/v1/chat",
                                  purpose="给纪要写摘要")

    def test_consent_lets_it_through(self) -> None:
        c = guard.CloudConsent(host="api.deepseek.com", purpose="摘要",
                               what="访谈纪要 1,200 字")
        try:
            guard.guarded_request("https://api.deepseek.com/v1/chat",
                                  data=b'{"x":1}', purpose="摘要", consent=c)
        except guard.EgressBlocked as e:
            self.fail(f"带授权时不该被拦：{e}")
        except Exception:
            pass                     # 连不上是另一回事，闸门已经放行

    def test_consent_is_host_bound(self) -> None:
        """**授权绑主机** —— 拿 A 家的授权去调 B 家不算数。"""
        c = guard.CloudConsent(host="api.deepseek.com", purpose="摘要", what="x")
        with self.assertRaises(guard.EgressBlocked):
            guard.guarded_request("https://api.openai.com/v1/chat",
                                  purpose="摘要", consent=c)

    def test_consent_is_purpose_bound(self) -> None:
        """**授权绑用途** —— 一次授权不能当万能通行证。"""
        c = guard.CloudConsent(host="api.deepseek.com", purpose="摘要", what="x")
        with self.assertRaises(guard.EgressBlocked):
            guard.guarded_request("https://api.deepseek.com/v1/chat",
                                  purpose="把整份材料发出去", consent=c)

    def test_consented_call_logs_what_went_out(self) -> None:
        """授权放行时，审计要记**发出去了多少、是不是同一份**。"""
        c = guard.CloudConsent(host="api.deepseek.com", purpose="摘要",
                               what="访谈纪要第 3 页")
        try:
            guard.guarded_request("https://api.deepseek.com/v1/chat",
                                  data=b'{"a":1}', purpose="摘要", consent=c)
        except guard.EgressBlocked:
            self.fail("不该被拦")
        except Exception:
            pass
        rec = [r for r in self._records() if r.get("decision") == "ALLOW-CONSENT"]
        self.assertEqual(len(rec), 1, self._records())
        self.assertIn("payload", rec[0])
        self.assertEqual(rec[0]["payload"]["bytes"], 7)
        self.assertIn("sha256", rec[0]["payload"])
        self.assertEqual(rec[0]["consent"]["what"], "访谈纪要第 3 页")

    def test_blocked_call_records_no_payload(self) -> None:
        """**被拦的请求不记内容摘要。**

        记了等于把「本来不该出去的东西」写进了审计文件 ——
        审计文件本身就成了第二个泄密点。
        """
        with self.assertRaises(guard.EgressBlocked):
            guard.guarded_request("https://api.deepseek.com/v1/chat",
                                  data="一份不该出去的机密内容".encode("utf-8"),
                                  purpose="摘要")
        rec = [r for r in self._records() if r.get("decision") == "BLOCK"]
        self.assertEqual(len(rec), 1)
        self.assertNotIn("payload", rec[0])
        self.assertIn("没有授权", rec[0]["reason"])

    def test_loopback_needs_no_consent(self) -> None:
        """本地模型永远不用授权 —— 零出境是默认状态。"""
        try:
            guard.guarded_request("http://127.0.0.1:11434/api/chat", data=b"{}",
                                  purpose="本地推理")
        except guard.EgressBlocked as e:
            self.fail(f"回环地址不该被拦：{e}")
        except Exception:
            pass
        self.assertEqual(self._records()[-1]["decision"], "ALLOW")


if __name__ == "__main__":
    unittest.main(verbosity=2)
