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


def _strip_css_comments(src: str) -> str:
    """去掉 CSS 注释再扫规则 —— 否则注释里写的反例会被当成真规则（我自己踩过）。"""
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


class TestHideUtilityAlwaysWins(unittest.TestCase):
    """`.hide` 必须永远能隐藏 —— 这一条是踩出来的（"按键无效/关不掉"）。

    2026-09-24：本机向导页里 `.hide{display:none}` 写在 `.onb{display:flex}` **前面**，
    两条同优先级 → 后写的赢 → 全屏引导遮罩**永远显示**；而 JS 那边 `onbKey()`
    第一句是"带 hide 就 return"，它以为自己是隐藏的 → 键盘、跳过、开始使用全部失效。
    用户被关在遮罩里，浏览器控制台**一行错都没有**（语法没错、逻辑也没错，是 CSS 赢了）。

    所以：工具类的显示规则必须带 !important，且每个页面都要有；谁把它去掉，这里就红。
    """

    def test_every_page_hide_rule_carries_important(self):
        for name in PAGES:
            p = ROOT / name
            if not p.exists():
                continue
            src = _strip_css_comments(p.read_text(encoding="utf-8"))
            m = re.search(r"\.hide\s*\{([^}]*)\}", src)
            if not m:
                continue
            body = m.group(1).replace(" ", "")
            self.assertIn("display:none", body, f"{name}: .hide 得真的隐藏({body})")
            self.assertIn("!important", body,
                          f"{name}: .hide 的 display 必须带 !important —— "
                          "同优先级的后置规则会盖掉它，遮罩就关不掉了")

    def test_overlay_has_a_backup_rule(self):
        page = (ROOT / "webapp_page.html").read_text(encoding="utf-8")
        self.assertIn(".onb.hide{display:none}", page.replace(" ", ""),
                      "遮罩要有第二条隐藏规则兜底（万一 !important 被误删）")

    def test_overlay_can_be_escaped_without_any_state(self):
        """`?onb=0` 与"点遮罩空白处"两条退路必须在 —— 遮罩不能把人关在里面。"""
        page = (ROOT / "webapp_page.html").read_text(encoding="utf-8")
        self.assertIn('onb=0', page)
        self.assertIn('e.target === el', page, "点空白处关闭那条要真的按目标判断")


def _lang_blocks(src: str) -> dict[str, str]:
    """把说明文件页的中文块 / 英文块切开。

    切完**要截到下一个语言块为止** —— 否则中文块会一直吃到文件尾，
    把英文块的标题也算进来（第一版就是这么错的，测试两边都看同一批 id，永远相等）。
    """
    out: dict[str, str] = {}
    for tag in ("zh", "en"):
        marker = f'<div class="lang-{tag}'
        if marker not in src:
            continue
        rest = src.split(marker, 1)[1]
        cut = len(rest)
        for other in ("zh", "en"):
            m = f'<div class="lang-{other}'
            if m in rest:
                cut = min(cut, rest.index(m))
        out[tag] = rest[:cut]
    return out


class TestDocLanguageBlocks(unittest.TestCase):
    """中文别插进英文块（真错过一次，而且肉眼看不出来）。

    2026-09-24：给说明文件页加「08 参考值从哪来 · 缺数怎么读」时，我把**中文那节**
    插进了 `<div class="lang-en">` 里 —— 锚点 `<h2 id="features-en">` 就在英文块内。
    后果：英文视图里会蹦出一整节中文（这个项目一直在防的"中英混屏"），
    而 `check_ui` 的中英块检查只看"两块都在不在"，查不出**错位**。

    这里钉一条不会误伤的规则：**英文块里的标题不许出现中日韩字符**。
    （正文里的中文示例是合理的 —— 比如举「购建固定资产」这种科目写法 —— 所以只管标题。）
    """

    def test_english_headings_have_no_cjk(self):
        src = (ROOT / "webapp_doc.html").read_text(encoding="utf-8")
        blocks = _lang_blocks(src)
        self.assertIn("en", blocks, "找不到英文块")
        en = blocks["en"].split("<script", 1)[0]
        bad = []
        for m in re.finditer(r"<h[23][^>]*>(.*?)</h[23]>", en, re.S):
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if re.search(r"[\u3000-\u9fff\uff00-\uffef]", title):
                bad.append(title)
        self.assertFalse(bad, f"英文块里的标题是中文：{bad}")

    def test_chinese_headings_are_chinese(self):
        src = (ROOT / "webapp_doc.html").read_text(encoding="utf-8")
        zh = _lang_blocks(src).get("zh", "")
        titles = [re.sub(r"<[^>]+>", "", m.group(1)).strip()
                  for m in re.finditer(r"<h[23][^>]*>(.*?)</h[23]>", zh, re.S)]
        self.assertTrue(any(re.search(r"[\u4e00-\u9fff]", x) for x in titles),
                        "中文块里一个中文标题都没有 —— 块可能被切错了")

    def test_both_blocks_have_the_same_section_ids(self):
        """中英两块的**节**必须一一对应（缺一节就是没写完 —— 说明书最容易只写一半）。

        只比 `<h2 id=...>`（节标题）—— 页脚那些 `ver`/`verline` 是共用元素，
        不是节，比它们会误报。
        """
        src = (ROOT / "webapp_doc.html").read_text(encoding="utf-8")
        blocks = _lang_blocks(src)
        zh_ids = set(re.findall(r'<h2 id="([a-z-]+)"', blocks.get("zh", "")))
        en_ids = set(re.findall(r'<h2 id="([a-z-]+)"', blocks.get("en", "")))
        en_top = {i[:-3] for i in en_ids if i.endswith("-en")}
        self.assertFalse(zh_ids - en_top, f"只有中文、没有英文的节：{sorted(zh_ids - en_top)}")
        self.assertFalse(en_top - zh_ids, f"只有英文、没有中文的节：{sorted(en_top - zh_ids)}")


if __name__ == "__main__":
    unittest.main()
