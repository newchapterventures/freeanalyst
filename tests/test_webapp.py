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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FITBIT = REPO / "materials" / "fitbit-2016-10k"


class TestBindAddress(unittest.TestCase):
    def test_only_loopback(self):
        self.assertEqual(webapp.HOST, "127.0.0.1")

    def test_page_has_six_steps(self):
        for n in ("1", "2", "5", "6"):
            self.assertIn(f'id="s{n}"', webapp.PAGE)

    def test_page_says_what_is_not_done(self):
        """第 3、4 步没做，页面上必须照实说 —— 不做成"看起来能用"。"""
        self.assertIn("未接", webapp.PAGE)
        self.assertIn("还没做", webapp.PAGE)


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
