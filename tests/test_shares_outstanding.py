"""取 A 股股数这条路径 —— **必须**过按次授权那道闸。

这是"新主机要用户同意"这条要求的**端到端证明**：
不是 `net.py` 里有个漂亮的机制，而是**真的有人在用、且真的被拦住**。

测试不碰用户真实的审计文件（`AUDIT_PATH` 指到临时文件），
也不真的出网（`urlopen` 被打桩）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import net
from datasources import prices


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _quote_body(total_shares: float = 1_250_081_601.0) -> bytes:
    return json.dumps({
        "rc": 0,
        "data": {"f43": 123673, "f57": "600519", "f58": "贵州茅台",
                 "f84": total_shares, "f85": total_shares,
                 "f116": total_shares * 1236.73},
    }).encode("utf-8")


class TestSharesOutstanding(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(net, "AUDIT_PATH", Path(self._tmp.name) / "pub.jsonl")
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self._tmp.cleanup)

    # ── 核心：没同意，就一个字节都不发 ────────────────────────────────────
    def test_without_consent_it_asks_instead_of_fetching(self):
        with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")) as u:
            with self.assertRaises(net.HostConsentRequired) as ctx:
                prices.shares_outstanding("sh", "600519")
        self.assertFalse(u.called, "★ 没同意就**不该发出去** —— 一个字节都不行")
        self.assertEqual(ctx.exception.host, "push2.eastmoney.com")
        self.assertIn("总股本", ctx.exception.purpose, "弹窗上要能看懂在取什么")

    # ── 带上同意 → 正常取到，并写进审计 ──────────────────────────────────
    def test_with_consent_it_returns_shares_and_audits_who_approved(self):
        consent = net.HostConsent("push2.eastmoney.com",
                                  net.CONSENT_HOSTS["push2.eastmoney.com"], "总股本")
        with net.host_consent([consent]):
            with mock.patch("net.urllib.request.urlopen",
                            return_value=_Resp(_quote_body())) as u:
                got = prices.shares_outstanding("sh", "600519")
        self.assertTrue(u.called)
        self.assertAlmostEqual(got.shares, 1_250_081_601.0)
        self.assertIn("东方财富", got.source)
        self.assertIn("非基准日当日", got.note,
                      "★ 口径要说清：这是当前股本，不是基准日那天的")

        events = [json.loads(ln) for ln in
                  (Path(self._tmp.name) / "pub.jsonl").read_text(encoding="utf-8").splitlines()
                  if ln.strip()]
        allowed = [e for e in events if e.get("decision") == "ALLOW-CONSENT"]
        self.assertTrue(allowed, "用户点头过的那次要在审计里留痕")
        self.assertEqual(allowed[-1]["consent"]["approved_by"], "user")

    # ── 同意**不跨调用**（按次）────────────────────────────────────────────
    def test_consent_does_not_leak_to_the_next_call(self):
        consent = net.HostConsent("push2.eastmoney.com",
                                  net.CONSENT_HOSTS["push2.eastmoney.com"], "总股本")
        with net.host_consent([consent]):
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(_quote_body())):
                prices.shares_outstanding("sh", "600519")
        with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")):
            with self.assertRaises(net.HostConsentRequired):
                prices.shares_outstanding("sh", "600519")   # 第二次：照样要问

    # ── 不猜、不充 ────────────────────────────────────────────────────────
    def test_missing_field_is_reported_not_papered_over(self):
        body = json.dumps({"rc": 0, "data": {"f43": 1, "f57": "600519"}}).encode()
        consent = net.HostConsent("push2.eastmoney.com",
                                  net.CONSENT_HOSTS["push2.eastmoney.com"], "总股本")
        with net.host_consent([consent]):
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(body)):
                with self.assertRaises(prices.PriceError) as ctx:
                    prices.shares_outstanding("sh", "600519")
        self.assertIn("f84", str(ctx.exception))
        self.assertIn("不拿别的字段充", str(ctx.exception))

    def test_hk_and_us_shares_are_refused_honestly(self):
        for mkt in ("hk", "us"):
            with self.subTest(market=mkt):
                with self.assertRaises(prices.PriceError) as ctx:
                    prices.shares_outstanding(mkt, "00700")
                self.assertIn("只接 A 股", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
