"""模型配置层 + 云端授权的测试。

盯住四件事，都是"看起来能用、实际不该放行"的那类：

1. **密钥不回显** —— 界面拿不到密钥原文，日志、JSON 里也不该出现。
2. **不带按次授权，云端一个字节都不许发** —— 这是「机密材料不出网」的底线。
3. **协议不兼容的服务商不许假装可用**（Anthropic / Gemini / Bedrock）。
4. **"测不了"不是"不合格"** —— 认证失败、超时不能被写成"未达门槛"。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from guard import CloudConsent  # noqa: E402
from llm import cloud, config as lcfg, gate  # noqa: E402
from llm.backends import ModelError  # noqa: E402


class TestProviders(unittest.TestCase):

    def test_registry_lists_both_kinds(self):
        ready = [n for n, i in cloud.PROVIDERS.items() if i["kind"] == "ready"]
        adapter = [n for n, i in cloud.PROVIDERS.items() if i["kind"] != "ready"]
        self.assertGreaterEqual(len(ready), 10, "可直接用的服务商太少")
        self.assertIn("deepseek", ready)
        self.assertIn("anthropic", adapter)
        self.assertIn("gemini", adapter)

    def test_adapter_providers_are_not_faked(self):
        """协议不同的服务商**不许**假装能用 —— 要说清需要写适配器。"""
        with self.assertRaises(ModelError) as ctx:
            cloud.build("anthropic")
        self.assertIn("适配", str(ctx.exception))

    def test_unknown_provider(self):
        with self.assertRaises(ModelError):
            cloud.build("不存在的服务商")

    def test_generate_requires_per_call_consent(self):
        """没有授权 → 拒绝发送（不是静默降级、也不是悄悄发出去）。"""
        b = cloud.CloudBackend(provider="deepseek",
                               base_url="https://api.deepseek.com", api_key="sk-test")
        with self.assertRaises(ModelError) as ctx:
            b.generate("deepseek-chat", "你好")
        self.assertIn("按次授权", str(ctx.exception))

    def test_consent_for_another_host_is_rejected(self):
        """拿 A 主机的授权去发 B 主机 —— 不算数。"""
        b = cloud.CloudBackend(provider="deepseek",
                               base_url="https://api.deepseek.com", api_key="sk-test")
        wrong = CloudConsent(host="api.openai.com", purpose="别的用途", what="别的材料")
        with self.assertRaises(ModelError) as ctx:
            b.generate("deepseek-chat", "你好", consent=wrong)
        self.assertIn("不匹配", str(ctx.exception))

    def test_models_are_static_never_fetched(self):
        """列模型**不发请求** —— 少一次多余的出网就少一次泄露机会。"""
        b = cloud.CloudBackend(provider="deepseek", base_url="https://api.deepseek.com",
                               api_key="sk-test")
        self.assertTrue(b.list_models())
        self.assertEqual(b.list_models(), list(cloud.PROVIDERS["deepseek"]["models"]))


class TestConfigStore(unittest.TestCase):

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "llm.json"
            cfg = lcfg.load(p)
            cfg["purposes"]["ask"] = "qwen3:14b"
            cfg["cloud"]["deepseek"]["enabled"] = True
            lcfg.save(cfg, p)
            again = lcfg.load(p)
        self.assertTrue(again["cloud"]["deepseek"]["enabled"])
        self.assertEqual(again["purposes"]["ask"], "qwen3:14b")

    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = lcfg.load(Path(d) / "就没有这个文件.json")
        self.assertIn("local", cfg)
        self.assertFalse(cfg["cloud"]["deepseek"]["enabled"], "云端默认必须是关的")

    def test_broken_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "llm.json"
            p.write_text("{ 这不是 json", encoding="utf-8")
            cfg = lcfg.load(p)
        self.assertIn("purposes", cfg)

    def test_masked_hides_the_key(self):
        cfg = {"cloud": {"deepseek": {"api_key": "sk-abcdefghijklmnop", "enabled": True}},
               "local": {}, "purposes": {}}
        m = lcfg.masked(cfg)
        blob = json.dumps(m, ensure_ascii=False)
        self.assertNotIn("sk-abcdefghijklmnop", blob)
        self.assertTrue(m["cloud"]["deepseek"]["has_key"])
        self.assertNotIn("api_key", m["cloud"]["deepseek"])

    def test_file_permissions_are_600(self):
        """里面有密钥 —— 权限必须是 600（别人读不到）。"""
        with tempfile.TemporaryDirectory() as d:
            p = lcfg.save(lcfg.load(Path(d) / "x.json"), Path(d) / "llm.json")
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)


class TestKeyFileReading(unittest.TestCase):
    """从 `~/.keys/*.md` 这种笔记文件里取密钥：**只按模式挑，别的内容不碰。**"""

    def test_picks_the_key_out_of_prose(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "notes.md"
            p.write_text("# DeepSeek\n\n备注：别的信息\n\nkey: sk-abcdefghijklmnopqrstuvwxyz\n",
                         encoding="utf-8")
            got = cloud.read_key_file(p)
        self.assertEqual(got, "sk-abcdefghijklmnopqrstuvwxyz")

    def test_missing_file_returns_empty(self):
        self.assertEqual(cloud.read_key_file("/tmp/没有这个文件.md"), "")
        self.assertEqual(cloud.read_key_file(""), "")

    def test_mask_keeps_it_unreadable(self):
        """打码后不能还原出原文（只剩前 6 后 4）。"""
        m = cloud.mask("sk-abcdefghijklmnopqrstuvwxyz")
        self.assertTrue(m.startswith("sk-abc"))
        self.assertTrue(m.endswith("wxyz"))
        self.assertNotIn("defghij", m)


class TestGateCannotVersusFailed(unittest.TestCase):
    """"测不了" ≠ "不合格"。

    实测踩到：千问那次全部用例都是 `HTTP Error 401`（密钥文件里没有可用的 key），
    而裁定写的是"未达门槛 ✗" —— 人会据此把那个模型否掉，
    而问题其实在密钥上。这类**假裁定**比没有结论更坏。
    """

    def _report(self, d: Path, error: str = "") -> Path:
        p = d / "r.json"
        p.write_text(json.dumps({"m": {
            "passed": 0, "total": 2, "error": error,
            "results": [
                {"name": "单位纪律", "passed": False, "reasons": ["调用失败：HTTPError: HTTP Error 401"]},
                {"name": "主体识别", "passed": False, "reasons": ["调用失败：HTTPError: HTTP Error 401"]},
            ]}}, ensure_ascii=False), encoding="utf-8")
        return p

    def test_all_calls_failed_becomes_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            r = gate._from_json("m", self._report(Path(d)))
        self.assertIsNotNone(r)
        assert r is not None
        self.assertIn("401", r.error)
        self.assertIn("测不了", r.line())          # 不是"未达门槛"

    def test_real_failures_stay_verdicts(self):
        """真的答错了（有答案、有理由）→ 仍然报"未达门槛"。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text(json.dumps({"m": {
                "passed": 0, "total": 1, "results": [
                    {"name": "主体识别", "passed": False,
                     "reasons": ["把实控人的义务说成了公司义务"],
                     "answer": "公司需承担回购义务…"}],
            }}, ensure_ascii=False), encoding="utf-8")
            r = gate._from_json("m", p)
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r.error, "")
        self.assertIn("主体识别", " ".join(r.failed))


class TestBenchExitCodes(unittest.TestCase):
    """退出码："测不了"与"不合格"不许共用一个码。

    实测踩到：千问 401 那次，`model_quality.py` 退出码是 2（"没达门槛"）。
    自动化里这两种情况要采取的动作完全不同 —— 一种是修密钥，一种是换模型。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "bench"))
        import model_quality as mq          # noqa: PLC0415
        cls.mq = mq

    def test_all_pass_returns_zero(self):
        r = {"a": {"passed": 5, "total": 5}}
        self.assertEqual(self.mq.exit_code(r), 0)

    def test_real_miss_returns_two(self):
        r = {"a": {"passed": 4, "total": 5, "error": ""}}
        self.assertEqual(self.mq.exit_code(r), 2)

    def test_cannot_run_returns_three(self):
        r = {"a": {"passed": 0, "total": 5, "error": "调用失败：HTTP Error 401"}}
        self.assertEqual(self.mq.exit_code(r), 3)

    def test_a_real_miss_outranks_a_cannot_run(self):
        """两个混在一起时按"真没达门槛"报（有人真的没通过，这是更强的信号）。"""
        r = {"a": {"passed": 0, "total": 5, "error": "调用失败：HTTP Error 401"},
             "b": {"passed": 3, "total": 5, "error": ""}}
        self.assertEqual(self.mq.exit_code(r), 2)


class TestModelGuide(unittest.TestCase):
    """哪台机器配哪个模型 —— 指引的三条纪律。

    ① 不许编数（没量过就说没量过）；② "能装下"不等于"用着顺"，两件事分开说；
    ③ 数据只有一份来源（`llm/guide.py`），页面和说明文件都从它渲染。
    """

    def test_tiers_cover_the_no_gpu_laptop_case(self):
        from llm import guide

        rams = [t["ram"] for t in guide.NO_GPU_TIERS]
        self.assertIn("8 GB", rams, "最常见的商务本内存档位不能缺")
        self.assertIn("16 GB", rams)
        for t in guide.NO_GPU_TIERS:
            for k in ("ram", "rec", "file", "examples", "expect", "verdict"):
                self.assertTrue(t.get(k), f"{t.get('ram')} 档缺 {k}")

    def test_experience_thresholds(self):
        from llm import guide

        self.assertEqual(guide.experience(40)[0], "顺畅")
        self.assertEqual(guide.experience(15)[0], "可用")
        self.assertEqual(guide.experience(6)[0], "能忍")
        self.assertEqual(guide.experience(1)[0], "别用")
        self.assertEqual(guide.experience(0), ("", ""), "量不出来就说不知道")

    def test_fits_leaves_room_for_the_browser(self):
        """16GB 机器跑 5GB 驻留可以；8GB 机器跑同一个就不该说"可以"。"""
        from llm import guide

        ok8, why8 = guide.fits(5.0, 8.0)
        self.assertFalse(ok8, why8)
        ok16, _ = guide.fits(5.0, 16.0)
        self.assertTrue(ok16)

    def test_no_measurement_is_not_invented(self):
        from llm import guide

        missing = Path("/tmp/绝对没有这个文件-也许吧.json")
        self.assertEqual(guide.measured_rows(missing), [])
        lines = " ".join(guide.summary_lines(missing))
        self.assertIn("measure_models", lines, "没量过要说清怎么量")

    def test_measured_rows_marks_fit_and_feel(self):
        from llm import guide

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps({
                "machine": {"ram_gb": 16, "chip": "Apple M2"},
                "models": [
                    {"name": "a", "file_gb": 4.7, "resident_gb": 5.2, "tok_s": 18.0},
                    {"name": "b", "file_gb": 18.0, "resident_gb": 19.5, "tok_s": 3.0},
                    {"name": "c", "error": "装载失败：ConnectionError"},
                ]}, ensure_ascii=False), encoding="utf-8")
            rows = guide.measured_rows(p)
        by = {r["name"]: r for r in rows}
        self.assertTrue(by["a"]["fits"])
        self.assertEqual(by["a"]["feel"], "可用")
        self.assertFalse(by["b"]["fits"])
        self.assertEqual(by["b"]["feel"], "别用")
        self.assertFalse(by["c"]["fits"], "跑不起来的不许说装得下")
        self.assertIn("装载失败", by["c"]["fit_note"])

    def test_config_page_renders_the_guide(self):
        """指引用的是同一份数据 —— 页面上那几句话说得到，也说得出。"""
        import webapp

        html = webapp.config_html()
        for token in ("哪台机器配哪个模型", "没有独立显卡", "估值这一步不吃模型",
                      "本机实测", "驻留内存"):
            self.assertIn(token, html, token)
        d = webapp.api_config()
        self.assertIn("guide", d)
        self.assertTrue(d["guide"]["tiers"])
        self.assertTrue(d["guide"]["howto"])
        self.assertIsInstance(d["guide"]["measured"], list)


    def test_no_model_paths_cover_the_three_options(self):
        """没有本地模型的人：三条路要齐，且第一条必须是"什么都不装"。"""
        from llm import guide

        self.assertEqual(len(guide.NO_MODEL_PATHS), 3)
        first = guide.NO_MODEL_PATHS[0]
        self.assertIn("什么都不装", first["title"])
        for p in guide.NO_MODEL_PATHS:
            for k in ("title", "for", "cost", "how"):
                self.assertTrue(p.get(k), f"{p.get('title')} 缺 {k}")
        # 装本机那条必须给出可执行的安装路径
        install = " ".join(guide.INSTALL_STEPS)
        self.assertIn("ollama.com", install)
        self.assertIn("ollama pull", install)

    def test_recommend_scales_with_ram(self):
        from llm import guide, registry

        # **8GB 与 16GB 都推 4b**：它是实测过门槛的那个（5/5，3.4GB）；
        # 内存更大不等于该换更大的模型 —— 先过门槛，再看速度。
        self.assertEqual(guide.recommend(8)["model"], "qwen3.5:4b")
        self.assertEqual(guide.recommend(16)["model"], "qwen3.5:4b")
        # 内存宽裕才上 9b（容量更大，但实测 4/5）
        self.assertEqual(guide.recommend(24)["model"], "qwen3.5:9b")
        self.assertEqual(guide.recommend(64)["model"], "qwen3.5:9b")
        # 推荐清单与 registry 的候选**不许走散** —— 两处各写一套，早晚对不上
        for ram in (8, 16, 24, 32):
            rec = guide.recommend(ram)["model"]
            self.assertIn(rec, registry.CANDIDATES[registry.tier_for(ram)],
                          f"{ram}GB 推荐的 {rec} 不在 registry 候选里")
        unknown = guide.recommend(0)
        self.assertTrue(unknown["command"].startswith("ollama pull "))
        self.assertIn("没探到", unknown["note"], "内存未知要说清为什么给这一档")

    def test_recommend_never_invents_a_score(self):
        """测过的必须带成绩；**没测过的不许编一个**。"""
        from llm import guide

        rec = guide.recommend(16)
        q = guide.quality_of(rec["model"])
        if q:
            self.assertIn(q["score"], rec["note"])
        else:
            self.assertNotIn("门槛", rec["note"])
        # 不钉死分数：重测就会变（14b 因为换调用路径从 4/5 变 3/5）。
        # 钉的是形状 —— 成绩必须来自真跑过的文件。
        self.assertIn(guide.quality_of("qwen3:14b")["score"], ("3/5", "4/5"))

    def test_detect_ram_never_guesses(self):
        """探不到就是 0 —— 不许编一个内存数。"""
        from llm import guide

        ram = guide.detect_ram_gb()
        self.assertIsInstance(ram, float)
        self.assertGreaterEqual(ram, 0)

    def test_config_page_contract_for_no_model_users(self):
        """页面缺字段 = 那段指引静默不渲染（这就是"看起来有、实际没有"）。"""
        import webapp

        d = webapp.api_config()
        g = d["guide"]
        for k in ("ram_gb", "recommend", "no_model_paths", "install_steps"):
            self.assertIn(k, g, k)
        self.assertTrue(g["recommend"]["command"])
        html = webapp.config_html()
        for token in ("no_model_paths", "install_steps", "recommend.command", "noModel",
                      "truth", "quality"):
            self.assertIn(token, html, token)


    def test_quality_table_reads_the_real_runs(self):
        """成绩必须来自跑过的文件 —— 页面上的每一行都要能追到一次真跑。

        **不在这里钉死具体分数**：每次重测分数就会变（14b 从 4/5 变 3/5，
        仅仅因为本机调用路径从 /v1 换成原生 + 关掉思考），钉死分数等于让
        "重新量一次"必然弄红测试 —— 那样人就会去改测试，而不是看数据。
        这里只钉形状：有分、有总分、没过就写清卡在哪几项、云端的能认出来。
        """
        from llm import guide

        rows = guide.quality_rows()
        self.assertTrue(rows, "bench/gate-*.json 里应该有成绩")
        by = {r["model"]: r for r in rows}
        self.assertIn("qwen3:14b", by)
        r14 = by["qwen3:14b"]
        self.assertEqual(r14["total"], 5)
        self.assertGreaterEqual(r14["passed"], 0)
        self.assertFalse(by["deepseek/deepseek-chat"]["local"], "带服务商前缀的是云端")
        if r14["passed"] < r14["total"] and not r14["error"]:
            self.assertTrue(r14["failed"], "未过的要写出卡在哪几项")
        self.assertIn("qwen3.5:4b", by, "当前一代要在表里（它就是过门槛的那个）")

    def test_truth_reports_that_a_local_model_passes(self):
        """**最该先说清的一句**：现在本机有过的了（qwen3.5:4b 5/5），代价也要说清。

        这条断言随实测变过 —— 2026-09 修好本机调用路径（思考型模型在 /v1 上关不掉思考，
        答案全被挤进 reasoning、content 恒空）之后重测，4b 才第一次过门槛。
        写测试的意义就在这：**结论变了，测试必须跟着变**，而不是把旧结论钉死。
        """
        from llm import guide

        txt = " ".join(guide.truth_lines())
        self.assertIn("能过门槛", txt)
        self.assertIn("qwen3.5:4b", txt, "过门槛的模型名字要报出来")
        self.assertIn("人工", txt, "过门槛也要说\"关键结论人工核对\"")
        self.assertIn("不等于", txt, "大不等于稳，这条也得说")
        self.assertIn("deepseek", txt.lower(), "有过门槛的要报出来（连同它的代价）")

    def test_truth_still_says_assistant_when_nothing_passes(self):
        """一场空的时候仍要说\"没有一个过门槛、只能当辅助\"—— 不能让新分支把话说丢了。"""
        from llm import guide

        rows = [{"model": "x:1b", "passed": 2, "total": 5, "score": "2/5",
                 "verdict": "未达门槛", "error": "", "failed": ["主体识别"], "local": True},
                {"model": "p/m", "passed": 5, "total": 5, "score": "5/5",
                 "verdict": "够格", "error": "", "failed": [], "local": False}]
        orig = guide.quality_rows
        guide.quality_rows = lambda: rows
        try:
            txt = " ".join(guide.truth_lines())
        finally:
            guide.quality_rows = orig
        self.assertIn("没有一个过门槛", txt)
        self.assertIn("辅助", txt)

    def test_repeat_measurement_supersedes_and_does_not_duplicate(self):
        """同一模型重测过就以后测的为准 —— 一张表里不许出现两行自相矛盾的同一模型。

        真踩过：`qwen3:14b` 在旧文件里 4/5、新文件里 3/5，两张表并排显示，
        而两次走的不是同一条调用路径（/v1+思考 vs 原生+关思考）—— 放一起比大小是错的。
        """
        from llm import guide

        rows = guide.quality_rows()
        models = [r["model"] for r in rows]
        self.assertEqual(len(models), len(set(models)), f"有重复模型：{models}")
        # 最新那份文件里的 14b 是 3/5 → 表里就该是 3/5（不是旧文件的 4/5）
        by = {r["model"]: r for r in rows}
        self.assertEqual(by["qwen3:14b"]["score"], "3/5")

    def test_recommend_leads_with_the_model_that_passes(self):
        """选型顺序：先过门槛、再看内存 —— 别推荐一个没过门槛的档位。"""
        from llm import guide

        for ram in (8, 16):
            r = guide.recommend(ram)
            self.assertEqual(r["model"], "qwen3.5:4b", f"{ram}GB 该推过门槛的那个")

    def test_recommend_with_no_measurement_says_so(self):
        from llm import guide

        r = guide.recommend(0)
        self.assertTrue(r["command"].startswith("ollama pull "))


    def test_cannot_run_is_not_a_quality_verdict(self):
        """旧结果文件里没记 error 时，"全失败"不许被当成 0/5 的质量裁定。"""
        from llm import guide

        q = guide.quality_of("dashscope/qwen-plus")
        self.assertTrue(q, "qwen-plus 应该在表里")
        self.assertIn("401", q["error"], "401 要认成『测不了』")
        self.assertEqual(q["failed"], [], "测不了的不列『卡在哪几项』（那是质量裁定）")


    def test_mostly_empty_answers_is_not_a_verdict(self):
        """4/5 题回答为空 ≠ 模型答错 —— 那是没测成（思考占满 token 预算）。

        实测踩到：qwen3.5:9b 五道题四道"模型原文"是空的，裁定却写"未达门槛 1/5"。
        拿空回答判质量，等于没测。
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text(json.dumps({"m": {
                "passed": 1, "total": 5,
                "results": [
                    {"name": "单位纪律", "passed": True, "answer": "没问题"},
                    {"name": "主体识别", "passed": False, "answer": "",
                     "reasons": ["缺少「结论」分节，无法判分"]},
                    {"name": "跨文档冲突", "passed": False, "answer": "",
                     "reasons": ["未引用 CIM 片段"]},
                    {"name": "格式遵循", "passed": False, "answer": "",
                     "reasons": ["缺少分节「结论」"]},
                    {"name": "拒答幻觉", "passed": False, "answer": "",
                     "reasons": ["缺少「结论」分节，无法判分"]},
                ]}}, ensure_ascii=False), encoding="utf-8")
            r = gate._from_json("m", p)
        self.assertIsNotNone(r)
        assert r is not None
        self.assertIn("4/5 处回答为空", r.error)
        self.assertIn("测不了", r.line())

    def test_one_empty_answer_still_verdicts(self):
        """只有一道空 → 仍按成绩算（别把真不合格也说成测不了）。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text(json.dumps({"m": {
                "passed": 3, "total": 5,
                "results": [{"name": f"用例{i}", "passed": i < 3,
                             "answer": "" if i == 4 else "答案",
                             "reasons": ["答错了"] if i >= 3 else []}
                            for i in range(5)]}}, ensure_ascii=False), encoding="utf-8")
            r = gate._from_json("m", p)
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r.error, "")
        self.assertIn("3/5", r.line())


if __name__ == "__main__":
    unittest.main()
