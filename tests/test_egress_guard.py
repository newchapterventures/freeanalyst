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


if __name__ == "__main__":
    unittest.main(verbosity=2)
