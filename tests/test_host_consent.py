"""新主机的**按次授权** —— 用户 2026-09-29 的要求：

> 新主机什么时候去启动抓取，需要用户的同意

这一层跟"白名单"是**两回事**，每条都要有断言（说错了比没有更坏）：

1. 没带同意 → **拦住**，不是静默成功
2. 带上同意 → 放行，且审计记的是 `ALLOW-CONSENT`（事后能证明是用户放的）
3. **日常数据源不问**（日线、SEC 不在名单里）—— 免得变成"会喊狼来了"
4. 出了 `with` 范围就失效 —— 这才叫**按次**
5. 用途不符不算数
6. `FREEANALYST_EXTRA_HOSTS` 能加白名单，但**绕不过这一层**
7. **没有全局开关** —— 代码里不存在 `OPEN_ALL_HOSTS` 这类东西

测试**不碰**用户真实的审计文件（`AUDIT_PATH` 被指到临时文件）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import net


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _q(**params) -> net.PublicQuery:
    return net.PublicQuery(params=params, purpose="peer shares")


class TestHostConsent(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self._tmp.name) / "pub.jsonl"
        self._p = mock.patch.object(net, "AUDIT_PATH", self.audit)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _events(self) -> list[dict]:
        if not self.audit.exists():
            return []
        return [json.loads(ln) for ln in self.audit.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ── 1 / 2：拦与放 ────────────────────────────────────────────────────
    def test_new_host_without_consent_is_blocked_not_silently_allowed(self):
        with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")) as u:
            with self.assertRaises(net.HostConsentRequired) as ctx:
                net.guarded_get("https://push2.eastmoney.com/api/qt/stock/get", _q(secid="1.600519"))
        self.assertFalse(u.called, "★ 没同意就**不该发出去** —— 一个字节都不行")
        self.assertEqual(ctx.exception.host, "push2.eastmoney.com")
        self.assertTrue(ctx.exception.purpose, "弹窗要显示用途，不能只有主机名")

        ev = [e for e in self._events() if e.get("host") == "push2.eastmoney.com"]
        self.assertTrue(ev, "拦下来这件事本身也要进审计")
        self.assertEqual(ev[-1]["decision"], "CONSENT-REQUIRED")

    def test_with_consent_it_goes_through_and_audit_says_who_allowed(self):
        c = net.HostConsent("push2.eastmoney.com",
                            net.CONSENT_HOSTS["push2.eastmoney.com"], "总股本")
        with net.host_consent([c]):
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b'{"ok":1}')) as u:
                body = net.guarded_get("https://push2.eastmoney.com/api/qt/stock/get",
                                       _q(secid="1.600519"))
        self.assertEqual(body, b'{"ok":1}')
        self.assertTrue(u.called)
        ev = [e for e in self._events() if e.get("decision") == "ALLOW-CONSENT"]
        self.assertTrue(ev, "授权放行要在审计里留下 ALLOW-CONSENT")
        self.assertEqual(ev[-1]["consent"]["approved_by"], "user")
        self.assertEqual(ev[-1]["consent"]["what"], "总股本")

    # ── 3：日常源不许天天弹窗（防"会喊狼来了"）──────────────────────────
    def test_ordinary_sources_are_not_asked_about(self):
        for url in ("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/x",
                    "https://data.sec.gov/api/xbrl/companyconcept/x.json"):
            with self.subTest(url=url):
                self.assertFalse(net.host_needs_consent(url.split("/")[2]))
                with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")):
                    net.guarded_get(url, _q(code="600519"))   # 不该抛

    def test_every_consent_host_has_a_human_readable_purpose(self):
        for host, why in net.CONSENT_HOSTS.items():
            with self.subTest(host=host):
                self.assertGreaterEqual(len(why), 10, f"{host} 的用途说明太短，弹窗上看不懂")
                self.assertIn(host, net.DEFAULT_ALLOWED_HOSTS,
                              "按次授权的主机也必须在白名单里（两层是叠加的）")

    # ── 4：按次 —— 出了范围就失效 ─────────────────────────────────────────
    def test_consent_expires_outside_the_block(self):
        c = net.HostConsent("qt.gtimg.cn", net.CONSENT_HOSTS["qt.gtimg.cn"], "总市值")
        with net.host_consent([c]):
            self.assertIsNotNone(net.consent_for("qt.gtimg.cn"))
        self.assertIsNone(net.consent_for("qt.gtimg.cn"),
                          "★ 出了 with 就没了 —— 这才叫**按次**，而不是一次点头永久生效")
        with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")):
            with self.assertRaises(net.HostConsentRequired):
                net.guarded_get("https://qt.gtimg.cn/q=sh600519", _q(code="600519"))

    # ── 5：用途不符不算数 ────────────────────────────────────────────────
    def test_consent_with_a_different_purpose_does_not_count(self):
        wrong = net.HostConsent("push2.eastmoney.com", "别的事情", "总股本")
        with net.host_consent([wrong]):
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")):
                with self.assertRaises(net.HostConsentRequired):
                    net.guarded_get("https://push2.eastmoney.com/api/qt/stock/get",
                                    _q(secid="1.600519"))

    # ── 6：环境变量绕不过 ────────────────────────────────────────────────
    def test_env_var_can_extend_the_allowlist_but_not_bypass_consent(self):
        with mock.patch.dict("os.environ", {"FREEANALYST_EXTRA_HOSTS": "example.com"}):
            # 加进白名单的普通主机：直接放行（这本来就是那个环境变量的用途）
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")):
                net.guarded_get("https://example.com/x", _q(code="600519"))
            # 但按次授权那一层，环境变量**进不来**
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")):
                with self.assertRaises(net.HostConsentRequired):
                    net.guarded_get("https://push2.eastmoney.com/api/qt/stock/get",
                                    _q(secid="1.600519"))

    def test_env_var_cannot_smuggle_a_consent_host_past_the_layer(self):
        with mock.patch.dict("os.environ",
                             {"FREEANALYST_EXTRA_HOSTS": "push2.eastmoney.com"}):
            self.assertTrue(net.host_needs_consent("push2.eastmoney.com"))
            with mock.patch("net.urllib.request.urlopen", return_value=_Resp(b"{}")):
                with self.assertRaises(net.HostConsentRequired):
                    net.guarded_get("https://push2.eastmoney.com/api/qt/stock/get",
                                    _q(secid="1.600519"))

    # ── 7：没有全局开关 ──────────────────────────────────────────────────
    def test_there_is_no_global_switch(self):
        """一个全局开关会让「材料不出本机」失去意义 —— 装完就一直是开着的。

        ★ 只查**代码里的标识符**，不查注释与字符串。
          第一版直接 grep 源码，结果搜到的是**我自己写的那句注释**
          （"一个 `OPEN_ALL_HOSTS=1` 会让这句话失去意义"）—— 那不是违规，
          正是在说明为什么不这么做。会喊狼来了的校验比没有更坏。
        """
        import io
        import tokenize

        names: set[str] = set()
        src = Path(net.__file__).read_text(encoding="utf-8")
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)

        for bad in ("OPEN_ALL_HOSTS", "ALLOW_ALL", "SKIP_CONSENT", "NO_CONSENT"):
            with self.subTest(name=bad):
                self.assertNotIn(bad, names, f"{bad} 这类全局开关不该出现在代码里")

    def test_consent_is_not_persisted_anywhere(self):
        """按次授权**不写文件** —— 免得变成"一次点头、以后一直开"。"""
        src = Path(net.__file__).read_text(encoding="utf-8")
        block = src[src.index("class HostConsent"):src.index("def consent_for")]
        self.assertNotIn("json.dump", block)
        self.assertNotIn("write_text", block)
        self.assertNotIn("open(", block)


if __name__ == "__main__":
    unittest.main()
