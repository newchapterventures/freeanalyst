"""本地向导的测试 —— 界面不该重写任何判断，也不该泄漏到本机之外。

两条要盯住的：
1. **界面只是一层皮**：`api_scan` / `api_appraise` 出来的东西，必须和
   命令行那条路一致（同一份 `intake` / `value`），不许在界面层再算一遍。
2. **只绑 127.0.0.1**：绑 0.0.0.0 的话，同一个 WiFi 下任何人都能打开页面、
   进而读到这台机器上的尽调材料。这条是红线，所以拿测试钉住。
"""

from __future__ import annotations

import json
import re
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
    #: 网页向导收上来的写法：百分比字段只填数值（% 显示在框里）。
    #: 所以这里写 `2.45` 就等于 2.45% —— **这是界面层的约定**，
    #: 与 txt 清单那条路的 `0.0245` 等价（见 intake.normalize_percents 的注释）。
    ANSWERS = {
        "unit": "千美元",
        "risk_free": "2.45 @高:国债",
        "equity_risk_premium": "5.5 @高:ERP",
        "beta_unlevered": "1.10 @演示",
        "cost_of_debt": "4.5 @演示",
        "tax_rate": "35 @高:法定",
        "debt": "0 @高:无有息负债",
        "equity": "800000 @演示",
        "growth": "5, 5, 5 @演示",
        "ebitda_margin": "6, 6, 6 @演示",
        "da_pct_revenue": "1.76 @中:历史",
        "capex_pct_revenue": "3.62 @中:历史",
        "nwc_pct_revenue": "18.18 @中:历史",
        "terminal_growth": "2.5 @中",
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

    def test_percent_input_is_read_as_percent(self):
        """人在带 % 的框里填 2.45，引擎必须读成 2.45%（不是 245%）。

        这是 100 倍级的静默错误 —— 报告照样出得来，所以从 HTTP 那层一路查到落盘的配置。
        """
        with tempfile.TemporaryDirectory() as d:
            r = webapp.api_appraise(str(FITBIT), "", dict(self.ANSWERS), out_dir=str(d))
            self.assertTrue(r["ok"], r.get("error"))
            cfg = json.loads(Path(r["files"]["config"]).read_text(encoding="utf-8"))
        self.assertAlmostEqual(cfg["wacc"]["risk_free"]["value"], 0.0245)
        self.assertAlmostEqual(cfg["wacc"]["tax_rate"]["value"], 0.35)
        self.assertAlmostEqual(cfg["wacc"]["equity_risk_premium"]["value"], 0.055)
        # beta 不是百分比字段 —— 不许被补上 %
        self.assertAlmostEqual(cfg["wacc"]["beta_unlevered"]["value"], 1.10)

    def test_interface_does_not_recompute_anything(self):
        """界面出的报告必须和命令行那条路**逐字一致** —— 同一套函数，不重写。"""
        from value import run_report

        with tempfile.TemporaryDirectory() as d:
            r = webapp.api_appraise(str(FITBIT), "", self.ANSWERS, out_dir=str(d))
            mat = webapp._CACHE[str(FITBIT)]
            ans = {k: webapp.parse_answer(v) for k, v in self.ANSWERS.items()}
            # 与界面同一条规则：百分比字段补 %（这不是"再算一遍"，是同一个规整）
            webapp.intake.normalize_percents(mat, ans)
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
        self.assertIn("doc", d["pages"])
        self.assertIn("version", d)
        # 能力标记：接口都在、但**少了字段**的那种"半新"旧进程也要能发现。
        # 实测踩过：百分比字段加进引擎没重启，页面上那些框静默地没有 %。
        self.assertIn("percent-unit", d["features"])

    def test_page_checks_the_feature_flags(self):
        page = webapp.page_html()
        self.assertIn('feats.indexOf("percent-unit")', page)

    def test_doc_page_is_served(self):
        with urllib.request.urlopen(self._url("/doc"), timeout=10) as r:
            body = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertIn("说明文件", body)
        self.assertIn("免责声明", body)

    def test_workbench_links_to_the_doc(self):
        """向导页上必须有一个进得去的「说明文件」入口。"""
        page = webapp.page_html()
        self.assertIn('id="docLink"', page)
        self.assertIn('href="/doc"', page)
        self.assertIn("nav.doc", page)

    def test_branding_and_version_are_on_the_page(self):
        """字标贴算盘顶、署名贴算盘底（一上一下夹住算盘）；标语在其下；版本号在底部。"""
        page = webapp.page_html()
        # 署名必须在字标块内，而且是**静态文字**（中英相同，不该依赖 JS 才出现）
        seg = page.split('class="head-txt"', 1)[-1].split('class="doclink"', 1)[0]
        self.assertIn('class="byline">by New Chapter Ventures<', seg)
        # 上对齐/下对齐靠这两条 CSS 实现
        rule = page.split(".head-txt{", 1)[-1].split("}", 1)[0]
        self.assertIn("align-self:stretch", rule)
        self.assertIn("space-between", rule)
        self.assertIn('class="tagline-row"', page)          # 标语在整块下方
        self.assertIn('id="ver"', page)                     # 底部版本号
        self.assertIn("foot.version", page)
        # 版本号的唯一来源是服务端的 /api/health —— 页面不许自己写死一个号
        self.assertIn("VER = (h && h.version)", page)
        self.assertNotIn("FreeAnalyst v0.", page)

    def test_three_header_lines_share_one_left_edge(self):
        """三行字左对齐必须是**算出来的**，不是对齐出来的。

        这里曾经写死过 `.tagline-row{padding-left:54px}`（= 当年算盘宽 38 + 间距 16）。
        后来算盘改成按高度缩放，宽度变 27.6px，标语就悄悄右移 10px ——
        三行错开，而所有测试都是绿的。所以现在钉死：宽度与左缩进**同源**。
        """
        page = webapp.page_html()
        flat = "".join(page.split())
        self.assertIn("--logo-w:calc(var(--logo-h)*19/33)", flat)
        self.assertIn("width:var(--logo-w)", flat)
        self.assertIn("padding-left:calc(var(--logo-w)+var(--head-gap))", flat)
        # 标语要有静态中文兜底：JS 没跑那行就是空的，量都量不出来
        self.assertIn('data-i18n="tagline">本地估值向导', page)
        # 装饰性命令行与标语同一块话 → 必须在同一列里（缩进只允许有一处）
        row = page.split('class="tagline-row"', 1)[-1].split('<div class="pills"', 1)[0]
        self.assertIn('class="prompt"', row)
        self.assertNotIn("padding-left", page.split(".prompt{", 1)[-1].split("}", 1)[0])
        # 兜底宽高与 CSS 变量不许各说各话
        mh = re.search(r"--logo-h:(\d+)px", page)
        ms = re.search(r'<svg class="logo" width="(\d+)" height="(\d+)"', page)
        self.assertTrue(mh and ms, "找不到 --logo-h 或 svg 的兜底宽高")
        assert mh is not None and ms is not None      # 给类型检查器一个明确承诺
        self.assertEqual(ms.group(2), mh.group(1))

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

    def test_choice_fields_render_as_dropdowns(self):
        """能确定的字段在界面上必须是下拉，且收集答案时不能漏掉下拉。"""
        page = webapp.page_html()
        self.assertIn("<select data-key=", page)          # 封闭集合
        self.assertIn("datalist", page)                   # 建议值（可编辑下拉）
        self.assertIn("#questions input, #questions select", page)

    def test_percent_fields_show_the_percent_sign(self):
        """百分比字段：% 显示在框里、提示写清「只填数值」；界面**原样发送**不自己换算。

        换算只允许在引擎侧一处（intake.normalize_percents）——
        界面要是自己乘除，界面、服务端、命令行就有三份算术，早晚不一致。
        """
        page = webapp.page_html()
        self.assertIn('class="pctsuf"', page)              # 框里那个 %
        self.assertIn('q.unit === "%"', page)              # 按引擎给的 unit 判断
        self.assertIn("unit.pct", page)                    # 「只填数值」的提示
        self.assertIn("answers[i.dataset.key] = i.value.trim();", page)   # 原样发

    def test_every_origin_kind_is_rendered(self):
        """来源类别要画出来：财报 / 财报推算 / 外部 / 判断 —— 少一类人就会认错。"""
        page = webapp.page_html()
        for origin, cls in (("财报", "filing"), ("财报推算", "derived"),
                            ("外部", "external"), ("判断", "judgment")):
            self.assertIn(f'"{origin}"', page, origin)
            self.assertIn(f".org.{cls}", page, cls)
            self.assertIn(f"origin.{cls}", page, cls)
        self.assertIn("originTag(q.origin)", page)

    def test_api_passes_choices_through(self):
        """options 必须从引擎一路传到页面，否则界面上还是手打。

        直接调 api_scan() 时拿到的是元组（走 HTTP 才是 JSON 数组），
        所以两边都 list() 一下再比 —— 比的是内容，不是容器类型。
        """
        d = webapp.api_scan(str(FITBIT))
        q = {x["key"]: x for x in d["questions"]}
        self.assertEqual(list(q["stance"]["options"]), ["买方", "卖方", "中立"])
        self.assertTrue(q["unit"]["suggest"])

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


class TestOnboarding(unittest.TestCase):
    """首次引导（三屏）—— 给不看文档的人看的那一段。

    盯住三件事：

    1. **它必须是"默认藏着"的**：万一 JS 没跑起来（旧服务、脚本报错），
       整页不能被一层遮罩挡住 —— 宁可没有引导，也不能用不了。
    2. **三屏的中英文都在**（少一条，切到英文就露出 key 名）。
    3. 看完**记住**（localStorage），并且**能重看**（页脚有入口）——
       不然第一次手快点掉，就永远找不回来了。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = webapp.page_html()

    def test_overlay_starts_hidden(self):
        self.assertIn('<div class="onb hide" id="onb">', self.html,
                      "引导层不是默认隐藏的 —— JS 出问题时整页会被挡住")

    def test_all_three_screens_exist(self):
        for i in (1, 2, 3):
            for suffix in ("t", "b"):
                self.assertIn(f'"onb.{i}.{suffix}":', self.html, f"onb.{i}.{suffix}")

    def test_screens_are_bilingual(self):
        """中英各一套 —— 数一数每个 key 出现两次（zh 一次、en 一次）。"""
        for i in (1, 2, 3):
            for suffix in ("t", "b"):
                key = f'"onb.{i}.{suffix}":'
                self.assertEqual(self.html.count(key), 2, f"{key} 中英不成对")

    def test_buttons_and_replay(self):
        for token in ("onbNext", "onbSkip", "onbDots", "onb.replay", "ONB_KEY"):
            self.assertIn(token, self.html, token)
        self.assertIn("onbMaybeStart()", self.html, "boot 里没调起来，引导永远不会出现")


class TestConfigPage(unittest.TestCase):
    """模型配置页 —— 关键不是"页面好看"，是四件事不许出错：

    密钥不回显、云端不带授权不许发、协议不兼容不假装可用、"测不了"≠"不合格"。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = webapp.config_html()

    def test_page_has_the_sections(self):
        for token in ("本机模型", "云端模型", "哪个用途用哪个模型",
                      "现在支持哪些", "出网审计"):
            self.assertIn(token, self.html, token)

    def test_page_says_the_data_risk_out_loud(self):
        self.assertIn("闭源模型有数据风险", self.html)
        self.assertIn("离开了本机", self.html)
        self.assertIn("单独授权", self.html)          # 按次授权，不是总开关
        self.assertIn("没有一个", self.html)          # 「没有一个总开关」

    def test_page_states_what_is_not_done(self):
        """没做的要写明（Anthropic / Gemini / Bedrock 需要适配器）。"""
        self.assertIn("需要单独适配", self.html)
        self.assertIn("不假装能用", self.html)
        self.assertIn("只有「问答」这一处", self.html)

    def test_js_syntax_of_the_config_page(self):
        """页面 JS 语法要过 —— 白屏是看不出来的那种坏。"""
        import shutil
        import subprocess
        import tempfile
        exe = shutil.which("node")
        if not exe:
            self.skipTest("没装 node")
        script = self.html.split("<script>", 1)[-1].split("</script>", 1)[0]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(script)
            p = Path(f.name)
        try:
            r = subprocess.run([exe, "--check", str(p)], capture_output=True, text=True)
        finally:
            p.unlink(missing_ok=True)
        self.assertEqual(r.returncode, 0, r.stderr[:300])

    def test_api_config_never_returns_a_key(self):
        from llm import config as lcfg
        d = webapp.api_config()
        blob = json.dumps(d, ensure_ascii=False)
        full = lcfg.key_for(lcfg.load(), "deepseek")
        if full:
            self.assertNotIn(full, blob, "完整密钥不许下发到界面")
        for p in d["providers"]:
            self.assertIn("key_display", p)
            self.assertNotIn("api_key", p)


class TestDocPage(unittest.TestCase):
    """说明文件 —— 使用者要的六件事，少一件就是没写完。

    正文是**长文**（不是界面小字），所以这里检查的是"每一节都在"，
    而不是逐句比对 —— 逐句比对的测试只会让人不敢改文案。
    """

    @classmethod
    def setUpClass(cls):
        cls.doc = webapp.doc_html()

    def test_covers_the_six_required_topics(self):
        for topic in ("功能", "免责声明", "使用哪些模型", "使用方法",
                      "如何调试", "接入自己的大模型"):
            self.assertIn(topic, self.doc, f"说明文件缺一节：{topic}")

    def test_is_bilingual(self):
        # 英文块带着 hide（默认中文），所以只匹配到 class 开头，不写死整段
        self.assertIn('class="lang-zh', self.doc)
        self.assertIn('class="lang-en', self.doc)
        for en in ("Disclaimer", "Bring your own model", "Debugging",
                   "Which models are used"):
            self.assertIn(en, self.doc, f"英文版缺：{en}")

    def test_warns_about_closed_source_data_risk(self):
        """闭源模型的数据风险必须写明白 —— 这是他明确要求的一条。"""
        self.assertIn("数据风险", self.doc)
        self.assertIn("离开了本机", self.doc)
        self.assertIn("按次授权", self.doc)
        self.assertIn("leave this machine", self.doc)

    def test_says_arithmetic_is_not_done_by_a_model(self):
        """「算术绝不用模型」是这套东西的底线，说明文件里必须讲。"""
        self.assertIn("算术绝不用模型", self.doc)
        self.assertIn("arithmetic never goes through a model", self.doc)

    def test_links_back_to_the_workbench(self):
        self.assertIn('href="/"', self.doc)

    def test_doc_has_version_and_byline(self):
        self.assertIn('id="ver"', self.doc)
        self.assertIn("by New Chapter Ventures", self.doc)
        self.assertIn("/api/health", self.doc)              # 版本号取自服务端

    def test_missing_file_does_not_500(self):
        """说明文件丢了，页面也得给一句人话，而不是 500。"""
        old = webapp.DOC_PATH
        try:
            webapp.DOC_PATH = Path("/tmp/根本没有这个说明文件.html")
            self.assertIn("说明文件缺失", webapp.doc_html())
        finally:
            webapp.DOC_PATH = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
