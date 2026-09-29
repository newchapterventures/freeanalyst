"""取 A 股股数这条路径 —— **必须**过按次授权那道闸。

这是"新主机要用户同意"这条要求的**端到端证明**：
不是 `net.py` 里有个漂亮的机制，而是**真的有人在用、且真的被拦住**。

★ 主力源是腾讯（实测：东财 push2 从 urllib 一律拒连，三种 UA 全 `RemoteDisconnected`）。
  所以"没同意"时抛的是 `qt.gtimg.cn`，不是东财。

测试不碰用户真实的审计文件（`AUDIT_PATH` 指到临时文件），也不真的出网。
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


def _tx_body(price: float = 1235.58, cap_yi: float = 15459.88) -> bytes:
    """腾讯盘口格式：~ 分隔，序号 3 现价、序号 44 总市值（亿元）。

    字段位置是**用已知值反证**出来的（见 prices.py 里的注释），这里就是照它造。
    """
    parts = ["0"] * 50
    parts[1] = "贵州茅台"
    parts[2] = "600519"
    parts[3] = f"{price}"
    parts[44] = f"{cap_yi}"
    return ('v_sh600519="' + "~".join(parts) + '";').encode("gbk")


def _em_body(total_shares: float = 1_250_081_601.0) -> bytes:
    return json.dumps({"rc": 0, "data": {"f57": "600519", "f84": total_shares}}).encode()


def _ok_consents() -> list:
    return [net.HostConsent(h, net.CONSENT_HOSTS[h], "总股本")
            for h in ("qt.gtimg.cn", "push2.eastmoney.com")]


class TestSharesOutstanding(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(net, "AUDIT_PATH", Path(self._tmp.name) / "pub.jsonl")
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _events(self) -> list[dict]:
        p = Path(self._tmp.name) / "pub.jsonl"
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ── 核心：没同意，就一个字节都不发 ────────────────────────────────────
    def test_without_consent_it_asks_instead_of_fetching(self):
        with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")) as u:
            with self.assertRaises(net.HostConsentRequired) as ctx:
                prices.shares_outstanding("sh", "600519")
        self.assertFalse(u.called, "★ 没同意就**不该发出去** —— 一个字节都不行")
        self.assertEqual(ctx.exception.host, "qt.gtimg.cn", "主力源是腾讯")
        self.assertIn("总市值", ctx.exception.purpose, "弹窗上要能看懂在取什么")

    # ── 带上同意 → 正常取到，并写进审计 ──────────────────────────────────
    def test_with_consent_it_returns_shares_and_audits_who_approved(self):
        with net.host_consent(_ok_consents()):
            with mock.patch("net.urllib.request.urlopen",
                            return_value=_Resp(_tx_body())) as u:
                got = prices.shares_outstanding("sh", "600519")
        self.assertTrue(u.called)
        # 15,459.88 亿 ÷ 1,235.58 = 约 12.51 亿股
        self.assertAlmostEqual(got.shares / 1e8, 12.51, places=1)
        self.assertIn("腾讯", got.source)
        self.assertIn("非基准日当日", got.note,
                      "★ 口径要说清：这是当前股本，不是基准日那天的")

        allowed = [e for e in self._events() if e.get("decision") == "ALLOW-CONSENT"]
        self.assertTrue(allowed, "用户点头过的那次要在审计里留痕")
        self.assertEqual(allowed[-1]["consent"]["approved_by"], "user")

    # ── 退路：腾讯挂了要能落到东财 ───────────────────────────────────────
    def test_falls_back_to_eastmoney_when_tencent_fails(self):
        def only_em(url, *a, **kw):
            if "gtimg" in getattr(url, "full_url", str(url)):
                raise OSError("模拟腾讯挂了")
            return _Resp(_em_body())

        with net.host_consent(_ok_consents()):
            with mock.patch("net.urllib.request.urlopen", side_effect=only_em):
                got = prices.shares_outstanding("sh", "600519")
        self.assertIn("东方财富", got.source, "退路没生效")
        self.assertAlmostEqual(got.shares, 1_250_081_601.0)

    # ── 同意**不跨调用**（按次）────────────────────────────────────────────
    def test_consent_does_not_leak_to_the_next_call(self):
        with net.host_consent(_ok_consents()):
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(_tx_body())):
                prices.shares_outstanding("sh", "600519")
        with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")):
            with self.assertRaises(net.HostConsentRequired):
                prices.shares_outstanding("sh", "600519")   # 第二次：照样要问

    # ── 不猜、不充 ────────────────────────────────────────────────────────
    def test_field_layout_change_is_reported_not_guessed(self):
        """字段段数不够 → 报"位置可能变了"，**不硬算**。"""
        short = ('v_sh600519="' + "~".join(["0"] * 10) + '";').encode("gbk")
        with net.host_consent(_ok_consents()):
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(short)):
                with self.assertRaises(prices.PriceError) as ctx:
                    prices.shares_outstanding("sh", "600519")
        self.assertIn("字段", str(ctx.exception))

    def test_missing_field_is_reported_not_papered_over(self):
        body = json.dumps({"rc": 0, "data": {"f57": "600519"}}).encode()

        def em_without_f84(url, *a, **kw):
            if "gtimg" in getattr(url, "full_url", str(url)):
                raise OSError("模拟腾讯挂了")
            return _Resp(body)

        with net.host_consent(_ok_consents()):
            with mock.patch("net.urllib.request.urlopen", side_effect=em_without_f84):
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

    # ── 代码 → 市场（按号段，不猜）────────────────────────────────────────
    def test_market_of_code_uses_published_number_bands(self):
        for code, want in (("600519", "sh"), ("601398", "sh"), ("688111", "sh"),
                           ("000001", "sz"), ("002415", "sz"), ("300750", "sz"),
                           ("301029", "sz")):
            with self.subTest(code=code):
                self.assertEqual(prices.market_of_code(code), want)
        for bad in ("830799", "430047", "00700", "AAPL", "60051"):
            with self.subTest(code=bad):
                self.assertIsNone(prices.market_of_code(bad),
                                  "认不出来就 None，**不猜**")


if __name__ == "__main__":
    unittest.main()
