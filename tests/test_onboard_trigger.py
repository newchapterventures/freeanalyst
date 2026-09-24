"""引导该不该出现 —— 判断依据、以及它真的按依据办事。

规则是**三个信号都为否才弹**，而且信号尽量往"用过没有"上靠，不往"这个浏览器记没记住"上靠：

    ① 服务端 runs > 0     出过一次估值 → 用过就不用教
    ② 服务端 onboarded    引导回记过   → 看过不必再看
    ③ 浏览器 localStorage 兜底（服务端状态读不到时）

为什么不用 localStorage 当主依据：它按**站点**记，而这个服务端口会变
（8765 被占就往后换），换了端口浏览器就当它是新站点 → 引导**又弹一次**；
反过来清了缓存的人，会被拦一次教学。

这个测试用 node 跑页面里**同一段 JS**：三种"不该出现"的场景 + 一种"该出现"。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE = ROOT / "webapp_page.html"

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const script = src.split("<script>")[1].split("</script>")[0];
const scenario = JSON.parse(process.argv[3]);   // {usage:{...}, seen:"1"|""}

function mkEl(id){
  const cls = new Set(id === "onb" ? ["onb", "hide"] : []);
  return {
    id, innerHTML: "", textContent: "", value: "", style: {}, dataset: {},
    classList: {
      add: c => cls.add(c), remove: c => cls.delete(c),
      contains: c => cls.has(c),
      toggle: (c, on) => (on === undefined ? (cls.has(c) ? cls.delete(c) : cls.add(c))
                                           : (on ? cls.add(c) : cls.delete(c))),
    },
    addEventListener(){}, setAttribute(){}, closest(){ return this; },
    querySelector(){ return mkEl(id + ":child"); },
    querySelectorAll(){ return []; },
    scrollIntoView(){},
    _cls: cls,
  };
}
const els = {};
const get = id => (els[id] = els[id] || mkEl(id));
const health = {ok:true, version:"0.40", endpoints:["pick"], pages:["doc","config"],
                features:["percent-unit"], usage: scenario.usage};
const ctx = vm.createContext({
  document: {
    getElementById: get,
    querySelectorAll: () => [],
    querySelector: () => mkEl("q"),
    documentElement: {}, body: {style:{}},
    addEventListener(){},                 // 引导的键盘支持要它（← → / Esc）
  },
  localStorage: {getItem: k => (k === "fa.onboarded" ? scenario.seen : null),
                 setItem(){}, removeItem(){}},
  navigator: {language: "zh-CN"},
  location: {protocol: "http:", search: ""},       // 有后端（不是预览模式）
  fetch: async () => ({json: async () => health}),
  setTimeout, clearTimeout, console,
});
vm.runInContext(script, ctx);
// fetch 是异步的：等一轮微任务再读结果
setTimeout(() => {
  console.log(JSON.stringify({shown: !els["onb"]._cls.has("hide")}));
  process.exit(0);
}, 30);
"""


@unittest.skipUnless(shutil.which("node"), "没装 node")
class TestOnboardTrigger(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.js = cls.tmp / "h.js"
        cls.js.write_text(HARNESS, encoding="utf-8")

    def _run(self, usage: dict, seen: str = "") -> bool:
        arg = json.dumps({"usage": usage, "seen": seen})
        out = subprocess.run([shutil.which("node") or "node", str(self.js),
                              str(PAGE), arg],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        return json.loads(out.stdout.strip().splitlines()[-1])["shown"]

    def test_first_time_ever_shows(self):
        self.assertTrue(self._run({"onboarded": False, "runs": 0}, seen=""),
                        "第一次打开、没用过 —— 该弹却没弹")

    def test_used_before_never_shows(self):
        """出过一次估值就不该再教 —— 哪怕这个浏览器没记过（换了浏览器/清了缓存）。"""
        self.assertFalse(self._run({"onboarded": False, "runs": 1}, seen=""),
                         "已经跑过估值还被拦一次教学")

    def test_acknowledged_never_shows(self):
        self.assertFalse(self._run({"onboarded": True, "runs": 0}, seen=""))

    def test_browser_memory_is_the_fallback(self):
        self.assertFalse(self._run({"onboarded": False, "runs": 0}, seen="1"),
                         "服务端状态读不到时，浏览器记的那一笔该兜住")

    def test_missing_usage_field_does_not_crash(self):
        """老服务没有 usage 字段 → 退回浏览器记忆，不报错。"""
        self.assertFalse(self._run({}, seen="1"))


class TestUsageStateOnTheServer(unittest.TestCase):
    """服务端那一半：状态存在哪、什么时候累加。

    **测试必须把状态文件指到临时目录** —— 否则跑一次测试就把人真实的使用记录改了
    （真踩过：测试跑了 10 次 appraise，用户的 runs 直接变成 10，引导再也不弹了）。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["FREANALYST_UI_STATE"] = str(Path(cls.tmp) / "ui-state.json")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("FREANALYST_UI_STATE", None)

    def test_state_file_is_outside_the_repo(self):
        import webapp

        p = str(webapp.UI_STATE)
        self.assertIn(".freeanalyst", p, "使用状态要放在用户目录下，不进仓库")
        self.assertNotIn(str(ROOT), p)

    def test_env_override_wins(self):
        """测试隔离靠这个：环境变量指到哪，就写哪。"""
        import webapp

        p = webapp._ui_state_path()
        self.assertIn(self.tmp, str(p))

    def test_runs_counter_and_onboarded_flag(self):
        import webapp

        self.assertEqual(webapp.usage_state(),
                         {"onboarded": False, "runs": 0, "first_run": ""})
        webapp.note_appraise()
        webapp.note_appraise()
        s = webapp.usage_state()
        self.assertEqual(s["runs"], 2)
        self.assertTrue(s["first_run"])
        webapp.api_onboarded()
        self.assertTrue(webapp.usage_state()["onboarded"])

    def test_health_carries_usage(self):
        import webapp

        self.assertIn("onboarded", webapp.ENDPOINTS)
        self.assertIn("onboard-state", webapp.FEATURES)

    def test_page_posts_the_acknowledgement(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("/api/onboarded", page)
        self.assertIn("h && h.usage", page, "boot 里没把服务端状态传进去")


if __name__ == "__main__":
    unittest.main()
