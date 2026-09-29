"""数字格式化（页面里的 `fmtNum`）—— 守住「界面把数字抹平」这一类看不见的错。

## 为什么值得单独一个测试
实测踩到：`fmtNum` 原来用 `indexOf("f") >= 0` 判断"要不要按整数显示" ——
而 `.1f` / `.2f` 里**也含字母 f** ✗，于是**所有倍数都被四舍五入成整数**：

    P/E 10.82 → 显示「11」
    P/B 1.10  → 显示「1」

接口里的数是**对的**，界面把它抹平了，而且看起来还挺正常 ——
这正是本项目最不许出现的那类错（"看起来能用"）。而它**任何 Python 测试都看不见**：
那是渲染层的事。所以这个测试把那段 JS 抽出来，用 node 真跑一遍。

## 依赖
需要 `node`。没有就**跳过并说明**（不是失败）—— 沿用项目里
"测不了 ≠ 不合格"的一贯做法。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE = ROOT / "webapp_page.html"

#: 被测函数 + 用例：(入参, 期望)
CASES = [
    ((10.82, ".1f"), "10.8"),        # ★ 曾经显示成「11」
    ((1.1, ".2f"), "1.10"),          # ★ 曾经显示成「1」
    ((2.71, ".1f"), "2.7"),
    ((0.1234, ".2%"), "12.34%"),
    ((0.1234, ".1%"), "12.3%"),
    ((12345678.0, ",.0f"), "12,345,678"),   # 规模类：仍然不要小数
    ((None, ".1f"), "—"),
]


def _fmt_num_source() -> str:
    """把 `function fmtNum(...){...}` 整段抠出来（按大括号配对，不靠行号）。"""
    text = PAGE.read_text(encoding="utf-8")
    start = text.index("function fmtNum(")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("在 webapp_page.html 里找不到完整的 fmtNum 函数")


@unittest.skipUnless(shutil.which("node"), "本机没有 node —— 跳过（测不了≠不合格）")
class TestFmtNum(unittest.TestCase):

    def test_formats_match_the_spec(self):
        src = _fmt_num_source()
        script = (src + "\n"
                  f"const cases = {json.dumps([[list(v), want] for v, want in CASES], ensure_ascii=False)};\n"
                  "for (const [args, want] of cases) {\n"
                  "  const got = fmtNum(args[0], args[1]);\n"
                  "  console.log(JSON.stringify({got, want}));\n"
                  "}\n")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = [json.loads(line) for line in out.stdout.strip().splitlines()]
        self.assertEqual(len(rows), len(CASES))
        for (args, want), row in zip(CASES, rows):
            self.assertEqual(row["got"], row["want"],
                             f"fmtNum({args[0]!r}, {args[1]!r}) → {row['got']!r}，"
                             f"期望 {want!r}")
        # 上面第 1 条（.1f 不许变整数）和第 6 条（,.0f 仍然不许要小数）
        # 合起来就把「含 f 就当整数」这个错钉住了 —— 不必再加一条结构断言：
        # 那种"检查源码字符串"的测试要么永远匹配不上（永不报警），
        # 要么一改就误报，两种都比没有更坏。


if __name__ == "__main__":
    unittest.main()
