"""真跑配置页里的 JS —— 有些错静态检查看不出来，只能执行一遍。

盯两件事（都真踩过）：

1. **指引文本里的 `**加粗**` 必须转成 `<b>`**，否则页面上原样显示星号（看着像坏了）。
   页面用 `md()` 做这件事，凡是渲染 `llm/guide.py` 文本的地方都得用它。
2. **"一个本地模型都没有"这个状态**必须真渲染出三条路 + 一条能粘的命令 ——
   这段是给第一次用的人看的，缺字段就静默空着（"看起来有、其实没有"）。

跑法：用 node 在最小 DOM 桩里执行 `webapp_config.html` 里**同一个** `<script>`。
没装 node 就跳过（和 `check_ui.py` 的 JS 语法检查一个规矩：没验就说没验）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const script = src.split("<script>")[1].split("</script>")[0];
const payload = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const els = {};
const get = id => (els[id] = els[id] || {
  id, innerHTML: "", dataset: {},
  classList: { add(){}, remove(){}, toggle(){} },
  _tb: { innerHTML: "" },
  querySelector() { return this._tb; },
  querySelectorAll: () => [],
});
const ctx = vm.createContext({
  document: { getElementById: get, querySelectorAll: () => [],
              querySelector: () => ({ innerHTML: "" }), documentElement: { lang: "zh" } },
  localStorage: { getItem: () => "zh", setItem: () => {} },
  // 页面脚本末尾的启动代码会 load() → fetch("/api/config")。
  // 桩里不给 fetch 的话，node 会在那一行抛 ReferenceError 退出（错在桩，不在页面）。
  fetch: async () => ({ json: async () => payload }),
  console,
});
vm.runInContext(script, ctx);
vm.runInContext("globalThis.__a = { renderNoModel, renderGuide };", ctx);
const a = ctx.__a;

// ① 没有模型：这段必须渲染出来
a.renderNoModel({ runtimes: [{ name: "ollama", ok: false, why: "连不上", models: [] }],
                  guide: payload.guide });
const block = els["noModel"].innerHTML;

// ② 有模型：这段必须不显示（否则是噪音）
a.renderNoModel({ runtimes: [{ name: "ollama", ok: true, models: ["qwen3:14b"] }],
                  guide: payload.guide });
const whenHas = els["noModel"].innerHTML;

// ③ 指引整块
a.renderGuide(payload.guide);

console.log(JSON.stringify({
  block_len: block.length,
  has_all_three: ["什么都不装", "本机模型", "走云端"].every(x => block.includes(x)),
  has_command: block.includes(payload.guide.recommend.command),
  has_install: block.includes("ollama.com"),
  bare_asterisks: (block.match(/\*\*/g) || []).length
                    + (els["howto"].innerHTML.match(/\*\*/g) || []).length
                    + (els["tierTable"]._tb.innerHTML.match(/\*\*/g) || []).length,
  bold_ok: block.includes("<b>") || els["tierTable"]._tb.innerHTML.includes("<b>"),
  hidden_when_model_present: whenHas.length === 0,
  tiers_rows: (els["tierTable"]._tb.innerHTML.match(/<tr>/g) || []).length,
}));
"""


@unittest.skipUnless(shutil.which("node"), "没装 node")
class TestConfigPageRuns(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import webapp

        cls.page = ROOT / "webapp_config.html"
        cls.payload = {"guide": webapp.guide_payload()}
        cls.tmp = tempfile.mkdtemp()
        cls.js = Path(cls.tmp) / "harness.js"
        cls.js.write_text(HARNESS, encoding="utf-8")
        cls.cfg = Path(cls.tmp) / "payload.json"
        cls.cfg.write_text(json.dumps(cls.payload, ensure_ascii=False), encoding="utf-8")
        node = shutil.which("node") or "node"
        out = subprocess.run([node, str(cls.js), str(cls.page), str(cls.cfg)],
                             capture_output=True, text=True, timeout=120)
        cls.out = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
        cls.err = out.stderr[-400:]

    def test_it_ran(self):
        self.assertTrue(self.out, f"node 没跑通：{self.err}")

    def test_no_model_block_renders_the_three_paths(self):
        self.assertGreater(self.out.get("block_len", 0), 200,
                           "没有模型时那段指引没渲染出来")
        self.assertTrue(self.out.get("has_all_three"), "三条路没齐")
        self.assertTrue(self.out.get("has_command"), "没给出能直接粘的命令")

    def test_it_hides_itself_when_a_model_exists(self):
        self.assertTrue(self.out.get("hidden_when_model_present"),
                        "机器上已经有模型还显示「还没有模型」= 噪音")

    def test_no_literal_asterisks_survive(self):
        """`**加粗**` 要变成 <b>，不许原样显示星号。"""
        self.assertEqual(self.out.get("bare_asterisks"), 0,
                         "指引文本里有没被转义的 ** 标记")
        self.assertTrue(self.out.get("bold_ok"), "加粗一个都没渲染出来")

    def test_tier_table_has_all_tiers(self):
        self.assertGreaterEqual(self.out.get("tiers_rows", 0), 4)


if __name__ == "__main__":
    unittest.main()
