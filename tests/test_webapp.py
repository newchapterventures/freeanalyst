"""本地向导的测试 —— 界面不该重写任何判断，也不该泄漏到本机之外。

两条要盯住的：
1. **界面只是一层皮**：`api_scan` / `api_appraise` 出来的东西，必须和
   命令行那条路一致（同一份 `intake` / `value`），不许在界面层再算一遍。
2. **只绑 127.0.0.1**：绑 0.0.0.0 的话，同一个 WiFi 下任何人都能打开页面、
   进而读到这台机器上的尽调材料。这条是红线，所以拿测试钉住。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FITBIT = REPO / "materials" / "fitbit-2016-10k"


class TestBindAddress(unittest.TestCase):
    def test_only_loopback(self):
        self.assertEqual(webapp.HOST, "127.0.0.1")

    def test_page_has_six_steps(self):
        for n in ("1", "2", "5", "6"):
            self.assertIn(f'id="s{n}"', webapp.page_html())

    def test_page_says_what_is_not_done(self):
        """第 3、4 步没做，页面上必须照实说 —— 不做成"看起来能用"。"""
        page = webapp.page_html()
        self.assertIn("未接", page)
        self.assertIn("还没做", page)

    def test_page_is_bilingual_with_a_manual_switch(self):
        """中英双语 + 手动切换 —— 选择记在 localStorage。"""
        page = webapp.page_html()
        self.assertIn('data-lang="zh"', page)
        self.assertIn('data-lang="en"', page)
        self.assertIn("fa.lang", page)                    # 记住选的语言
        self.assertIn("const I18N", page)

    def test_copy_is_formal_not_colloquial(self):
        """界面文案用正式金融用语 —— 不要「丢材料 / 认材料 / 出报告」这类口语。

        规则：注释里可以举反例（说明为什么不要用），所以先把 HTML 注释剥掉再查。
        """
        import re
        page = re.sub(r"<!--.*?-->", "", webapp.page_html(), flags=re.S)
        for word in ("丢材料", "认材料", "出报告", "认出来了", "没认出来"):
            self.assertNotIn(word, page, f"界面文案里不该出现口语：{word}")

    def test_every_i18n_key_exists_in_both_languages(self):
        """缺一条译文 = 界面上多一块空白，所以两边必须一一对应。"""
        import re
        page = webapp.page_html()
        used = set(re.findall(r'data-i18n(?:-html|-ph)?="([^"]+)"', page))
        # 词边界不能少：closest(".card")、split("\n") 都含 t(" ，会被误当成 t("key")。
        used |= set(re.findall(r'(?<![\w.])t\("([^"]+)"\)', page))
        zh = set(re.findall(r'"([\w.]+)":', page.split("zh: {", 1)[1].split("\n  },", 1)[0]))
        en = set(re.findall(r'"([\w.]+)":', page.split("en: {", 1)[1].split("\n  }", 1)[0]))
        self.assertEqual(sorted(zh), sorted(en), "中英词表条数/键名不一致")
        self.assertFalse(used - zh, f"有 key 没写进词表：{sorted(used - zh)}")

    def test_served_page_is_the_design_file(self):
        """线上那一页就是 `webapp_page.html` 本身 —— 不存在"稿子和实现不一样"。"""
        page = webapp.page_html()
        self.assertIn('aria-label="算盘"', page)       # 像素算盘 logo
        self.assertIn("FREE<em>ANALYST</em>", page)     # 字标


class TestApiScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = webapp.api_scan(str(FITBIT))

    def test_recognizes_three_tables(self):
        found = {t["kind"]: t["found"] for t in self.d["tables"]}
        self.assertEqual(found, {"balance": True, "income": True,
                                 "cash_flow": True})

    def test_carries_unit_and_scope(self):
        self.assertEqual(self.d["unit"], "千美元")
        self.assertEqual(self.d["scope"], "合并")

    def test_carries_checks(self):
        self.assertTrue(self.d["checks"])
        self.assertTrue(any(c["ok"] for c in self.d["checks"]))

    def test_carries_questions(self):
        keys = {q["key"] for q in self.d["questions"]}
        self.assertIn("unit", keys)
        self.assertIn("growth", keys)

    def test_out_dir_is_not_inside_the_materials(self):
        """产物不落在材料库里 —— 材料目录是只读输入。"""
        self.assertNotIn(str(FITBIT), self.d["out_dir"])

    def test_unused_files_are_reported(self):
        self.assertTrue(self.d["unused"])

    def test_missing_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            webapp.api_scan("/tmp/根本没有这个文件.pdf")


class TestApiAppraise(unittest.TestCase):
    ANSWERS = {
        "unit": "千美元",
        "risk_free": "0.0245 @高:国债",
        "equity_risk_premium": "0.055 @高:ERP",
        "beta_unlevered": "1.10 @演示",
        "cost_of_debt": "0.045 @演示",
        "tax_rate": "0.35 @高:法定",
        "debt": "0 @高:无有息负债",
        "equity": "800000 @演示",
        "growth": "0.05, 0.05, 0.05 @演示",
        "ebitda_margin": "0.06, 0.06, 0.06 @演示",
        "da_pct_revenue": "0.0176 @中:历史",
        "capex_pct_revenue": "0.0362 @中:历史",
        "nwc_pct_revenue": "0.1818 @中:历史",
        "terminal_growth": "0.025 @中",
        "valuation_date": "2016-12-31",
    }

    def test_refuses_when_unit_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            dummy = Path(d) / "m"
            dummy.mkdir()
            (dummy / "a.htm").write_text(
                "<table><tr><td>货币资金</td><td>1</td></tr>"
                "<tr><td>资产总计</td><td>10</td></tr>"
                "<tr><td>负债合计</td><td>4</td></tr>"
                "<tr><td>所有者权益合计</td><td>6</td></tr></table>",
                encoding="utf-8")
            r = webapp.api_appraise(str(dummy), "", {"growth": "0.05"},
                                    out_dir=str(Path(d) / "out"))
            self.assertFalse(r["ok"])
            self.assertIn("单位", r["error"])

    def test_runs_and_writes_files(self):
        with tempfile.TemporaryDirectory() as d:
            r = webapp.api_appraise(str(FITBIT), "", self.ANSWERS,
                                    out_dir=str(Path(d)))
            self.assertTrue(r["ok"], r.get("error"))
            self.assertIn("估值报告", r["report"])
            self.assertTrue(Path(r["files"]["report"]).exists())
            self.assertTrue(Path(r["files"]["config"]).exists())

    def test_missing_inputs_are_reported_not_faked(self):
        """不给成本那一块，就在 missing 里说清楚，而不是悄悄跳过。"""
        with tempfile.TemporaryDirectory() as d:
            ans = {k: v for k, v in self.ANSWERS.items() if k != "terminal_growth"}
            r = webapp.api_appraise(str(FITBIT), "", ans, out_dir=str(Path(d)))
            self.assertTrue(r["ok"])
            self.assertTrue(any("terminal_growth" in m for m in r["missing"]))

    def test_interface_does_not_recompute_anything(self):
        """界面出的报告必须和命令行那条路**逐字一致** —— 同一套函数，不重写。"""
        from value import run_report

        with tempfile.TemporaryDirectory() as d:
            r = webapp.api_appraise(str(FITBIT), "", self.ANSWERS, out_dir=str(d))
            mat = webapp._CACHE[str(FITBIT)]
            ans = {k: webapp.parse_answer(v) for k, v in self.ANSWERS.items()}
            cfg, _ = webapp.intake.build_config(mat, ans)
            self.assertEqual(r["report"],
                             run_report(cfg, Path(d), statements=mat.statements))


class TestPortFallback(unittest.TestCase):
    """端口被占时要自己换一个 —— **不能让"打不开"成为第一印象。**

    实测第一次交付时用户看到的就是 `ERR_CONNECTION_REFUSED`。
    """

    def test_skips_a_taken_port(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        taken = srv.server_address[1]
        try:
            got = webapp.find_port("127.0.0.1", taken)
            self.assertNotEqual(got, taken, "被占的端口不该再被选")
            self.assertGreater(got, taken)
        finally:
            srv.server_close()

    def test_uses_the_wanted_port_when_free(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)   # 先拿一个空的
        free = srv.server_address[1]
        srv.server_close()                                            # 马上还回去
        self.assertEqual(webapp.find_port("127.0.0.1", free), free)

    def test_gives_up_with_a_message(self):
        with self.assertRaises(SystemExit):
            webapp.find_port("127.0.0.1", 1, tries=1)   # 1 号端口跑不了（特权）


class TestPickPath(unittest.TestCase):
    """选择路径 —— **不上传、不复制**。

    浏览器拿不到真实路径，上传就等于把机密材料复制一份到别处；
    所以改成让服务去调系统原生选择框，只回一个路径字符串。
    测试里把 subprocess 换掉，否则跑测试会弹一个选择框出来。
    """

    @staticmethod
    def _fake(returncode=0, stdout="", stderr=""):
        return mock.patch.object(webapp.subprocess, "run",
                                 return_value=mock.Mock(returncode=returncode,
                                                        stdout=stdout, stderr=stderr))

    def test_macos_returns_the_real_path(self):
        with mock.patch.object(webapp.sys, "platform", "darwin"), \
             self._fake(stdout="/Users/x/deals/target/\n"):
            r = webapp.pick_path("dir")
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "/Users/x/deals/target")   # 去掉尾部斜杠

    def test_only_returns_a_path_nothing_else(self):
        """返回体里只有 ok / path —— 没有内容、没有副本，材料一个字都没动。"""
        with mock.patch.object(webapp.sys, "platform", "darwin"), \
             self._fake(stdout="/tmp/t\n"):
            r = webapp.pick_path("dir")
        self.assertEqual(sorted(r.keys()), ["ok", "path"])

    def test_cancel_is_not_an_error(self):
        """用户按取消 → 不是错误，界面上不该弹红字。"""
        with mock.patch.object(webapp.sys, "platform", "darwin"), \
             self._fake(returncode=1, stderr="User canceled."):
            r = webapp.pick_path("dir")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("cancelled"))
        self.assertNotIn("error", r)

    def test_non_macos_says_so_instead_of_pretending(self):
        with mock.patch.object(webapp.sys, "platform", "linux"):
            r = webapp.pick_path("dir")
        self.assertFalse(r["ok"])
        self.assertIn("macOS", r["error"])

    def test_timeout_is_reported(self):
        import subprocess as sp
        with mock.patch.object(webapp.sys, "platform", "darwin"), \
             mock.patch.object(webapp.subprocess, "run",
                               side_effect=sp.TimeoutExpired("osascript", 600)):
            r = webapp.pick_path("dir")
        self.assertFalse(r["ok"])
        self.assertIn("超时", r["error"])


class TestHttpLayer(unittest.TestCase):
    """HTTP 那一层也测 —— **只测函数的话，路由写错发现不了。**

    服务器开在**测试进程里**（端口 0 = 随便给一个），跟着测试一起起、一起关：
    不需要后台进程，也不会留下野端口。
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self._url(path), data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    def test_health(self):
        with urllib.request.urlopen(self._url("/api/health"), timeout=10) as r:
            self.assertTrue(json.loads(r.read().decode())["ok"])

    def test_index_serves_the_page(self):
        with urllib.request.urlopen(self._url("/"), timeout=10) as r:
            body = r.read().decode()
            self.assertEqual(r.status, 200)
            self.assertIn("本地估值向导", body)
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")

    def test_health_advertises_endpoints(self):
        """健康检查要自报家门 —— 页面据此发现「服务是旧进程」。"""
        with urllib.request.urlopen(self._url("/api/health"), timeout=10) as r:
            d = json.loads(r.read().decode())
        self.assertTrue(d["ok"])
        self.assertIn("pick", d["endpoints"])
        self.assertIn("version", d)

    def test_page_has_a_banner_for_a_stale_service(self):
        """服务是旧进程时要明说 —— 实测点按钮只回 unknown endpoint 过。"""
        page = webapp.page_html()
        self.assertIn('id="stale"', page)
        self.assertIn("stale.banner", page)
        self.assertIn("showStale", page)

    def test_scan_endpoint(self):
        d = self._post("/api/scan", {"path": str(FITBIT)})
        self.assertTrue(d["ok"])
        self.assertEqual(d["unit"], "千美元")
        self.assertEqual(sum(1 for t in d["tables"] if t["found"]), 3)

    def test_scan_missing_path_is_400_with_a_message(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/scan", {"path": "/tmp/根本没有这个文件.pdf"})
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read().decode())
        self.assertIn("不存在", body["error"])

    def test_scan_without_path_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/scan", {})
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_endpoint_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/nope", {})
        self.assertEqual(ctx.exception.code, 404)

    def test_pick_endpoint_returns_a_path(self):
        """走 HTTP 的选择路径：只回路径字符串，**没有任何文件内容**。"""
        with mock.patch.object(webapp.sys, "platform", "darwin"), \
             mock.patch.object(webapp.subprocess, "run",
                               return_value=mock.Mock(returncode=0,
                                                      stdout="/Users/x/deals/t/\n",
                                                      stderr="")):
            d = self._post("/api/pick", {"kind": "dir"})
        self.assertTrue(d["ok"])
        self.assertEqual(d["path"], "/Users/x/deals/t")
        self.assertNotIn("files", d)

    def test_appraise_endpoint_end_to_end(self):
        """走 HTTP 出的报告，和直接调函数出来的必须一样（同一条路）。"""
        old_root = webapp.intake.ROOT
        with tempfile.TemporaryDirectory() as d:
            webapp.intake.ROOT = Path(d)          # 别把测试产物丢进仓库的 out/
            try:
                self._post("/api/scan", {"path": str(FITBIT)})
                r = self._post("/api/appraise", {"path": str(FITBIT), "unit": "",
                                                 "answers": dict(TestApiAppraise.ANSWERS)})
            finally:
                webapp.intake.ROOT = old_root
            self.assertTrue(r["ok"], r.get("error"))
            self.assertIn("估值报告", r["report"])
            self.assertTrue(Path(r["files"]["report"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
