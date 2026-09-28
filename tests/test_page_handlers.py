"""页面里的"按下去了但没反应"——一次性挡住这一整类错误。

真踩过（2026-09-24，用户报的"初始页面按键无效"，浏览器里就一行：
`Uncaught ReferenceError: setLang is not defined`）：
引导弹层里的语言按钮绑的是 `setLang(...)`，而这个页面只定义了 `applyLang(...)`
（`setLang` 是配置页那边的名字）。点下去**抛错、什么也不发生** —— 静态看代码看不出来，
`node --check` 也过（语法是对的，只是名字不存在）。

所以这里做一件事：**把页面里"会被点到的函数名"逐一对照"这个页面定义了什么"**：
  ① inline 属性：`onclick="foo()"` / `onchange=` / `oninput=`
  ② JS 绑定：`addEventListener("click", () => foo(` / `=> foo(`
只要名字没定义，测试就红 —— 并且直接告诉你是哪一行、哪个名字。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ("webapp_page.html", "webapp_config.html", "webapp_doc.html")

#: 浏览器/JS 自带的东西，不算"页面里没定义"
BUILTINS = {
    "alert", "atob", "btoa", "clearInterval", "clearTimeout", "confirm", "console",
    "decodeURIComponent", "document", "encodeURIComponent", "escape", "event", "fetch",
    "history", "JSON", "location", "Math", "Number", "navigator", "open", "parseFloat",
    "parseInt", "print", "prompt", "requestAnimationFrame", "scrollTo", "setInterval",
    "setTimeout", "String", "this", "window", "Array", "Object", "Promise", "Date",
    "RegExp", "Error", "Boolean", "isNaN", "Blob", "URL", "FormData", "localStorage",
}

SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)
#: 定义：function foo( / const foo = / let foo = / var foo = / window.foo =
DEF_RE = re.compile(r"(?:function\s+([A-Za-z_$][\w$]*)\s*\(|"
                    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=|"
                    r"window\.([A-Za-z_$][\w$]*)\s*=)")
#: inline 属性里的调用
INLINE_RE = re.compile(r"""on(?:click|change|input|submit|keydown)\s*=\s*["']\s*([A-Za-z_$][\w$]*)\s*\(""")
#: addEventListener("click", ... => foo(   /   (() => foo()
BOUND_RE = re.compile(r"""addEventListener\s*\(\s*["'][a-z]+["']\s*,[^)]*?=>\s*([A-Za-z_$][\w$]*)\s*\(""")


def _script(src: str) -> str:
    return "\n".join(SCRIPT_RE.findall(src))


def _defined(js: str) -> set[str]:
    out: set[str] = set()
    for m in DEF_RE.finditer(js):
        for g in m.groups():
            if g:
                out.add(g)
    return out


def _referenced(src: str, js: str) -> dict[str, list[str]]:
    """名字 → 出处（哪一类、哪个片段），方便报错时直接定位。"""
    found: dict[str, list[str]] = {}
    for m in INLINE_RE.finditer(src):
        found.setdefault(m.group(1), []).append(f"inline: {m.group(0)[:40]}")
    for m in BOUND_RE.finditer(js):
        found.setdefault(m.group(1), []).append(f"addEventListener: {m.group(0)[:60]}")
    return found


class TestPageHandlers(unittest.TestCase):
    def test_every_handler_is_defined_in_that_page(self):
        problems = []
        for name in PAGES:
            p = ROOT / name
            if not p.exists():
                continue
            src = p.read_text(encoding="utf-8")
            js = _script(src)
            defined = _defined(js) | BUILTINS
            for fn, where in sorted(_referenced(src, js).items()):
                if fn not in defined:
                    problems.append(f"{name}: `{fn}()` 被绑了但这一页没定义 —— {where[0]}")
        self.assertFalse(problems, "点了没反应的死按钮：\n  " + "\n  ".join(problems))

    def test_the_two_language_functions_do_not_get_confused(self):
        """页面用 `applyLang`、配置页用 `setLang` —— 名字换错就会变成死按钮（真踩过）。"""
        page = (ROOT / "webapp_page.html").read_text(encoding="utf-8")
        cfg = (ROOT / "webapp_config.html").read_text(encoding="utf-8")
        self.assertIn("function applyLang(", page)
        self.assertNotIn("setLang(", page, "本机向导页没有 setLang —— 绑它等于死按钮")
        self.assertIn("function setLang(", cfg)
        self.assertIn("applyLang(", cfg)


if __name__ == "__main__":
    unittest.main()
