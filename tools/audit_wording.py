#!/usr/bin/env python3
"""列出**需要人工过目的话**：工具输出里那些"它其实没法证明"的说法。

    python3 tools/audit_wording.py

## 为什么是工具，不是测试（这条很重要）

**一个会喊狼来了的守卫，比没有守卫更坏。**

这套检查的命中里，**大部分是合理的**：
· "实测某白酒公司…" —— 真的测过；
· "该现金流量表用直接法编" —— 前面就有 `is_direct_method(pairs)` 在判；
· "永续增长的硬上界是长期 GDP 增速" —— 数学上成立；
· "通常/常见" —— 已经带了限定，是诚实的说法。

把它们当"错误"报红，人很快就会学会无视红字 —— 那时**真出问题也看不见了**。
所以这里**只列清单、不判对错、永远返回 0**：看完由人决定哪一句要改。

## 两个物种（今天各栽过一次）
① **断言词**（永远 / 总是 / 一定是 / 必然 / 一律 …）——
   实例："期中惯例**永远**算出更高的估值" ✗（现金流为负时反而更低）。
② **断言材料结构**（A 股 / H 股 / 年报里 / 惯例 …）——
   这一种**最危险：一个断言词都没有也能把材料说错**。
   实例："年报里合并表排在母公司表前面" ✗ —— 那是**惯例**，不成文、不可核对。
   好的做法是**先有检查**（`is_direct_method` 那样）或**带限定**（"通常"）。

## 只审"给用户看的话"
用 `ast` 抽**字符串字面量**：注释、docstring、测试夹具都不算 ——
它们不给用户看，审了只会淹没有效信息。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 不扫的目录：测试夹具是噪声、本工具自己含关键词（会自报）。
SKIP_DIRS = {".git", "out", "__pycache__", "node_modules", "materials", ".venv",
             "tests", "tools", "docs"}

#: ① 断言词 —— 说这些话时必须**真能证明**
CLAIMS = re.compile(r"永远|总是|一定是|必然|肯定|绝对|众所周知|显然|理所当然|"
                    r"绝不会|一律|无一例外")

#: ② 断言"材料长什么样" —— 每一句都该**有检查在前**，或带明确限定
STRUCT = re.compile(r"A\s?股|H\s?股|港股|年报里|审计报告里|惯例|通常|一般情况下|"
                    r"绝大多数|普遍|按规矩|按行业|往往")

#: 已经把话说住的限定词：命中它们就不标"未加限定"
HEDGED = re.compile(r"通常|常见|一般|往往|多数|可能|通常不|不一定|大致|左右")
#: **最强的一种依据**：材料里真量过。审的时候优先当"已交代清楚"看。
MEASURED = re.compile(r"实测|已测|量过|验证过|核对过")

HEADER = """待过目清单（**不是错误清单**）—— 命中大多数是合理的，逐条看完自己决定。

判据：① 断言词（永远/总是/一律…）② 断言材料结构（A股/H股/年报里/惯例…）。
带"通常/常见/可能"这类限定词的会自动标注 [已限定]。"""


def _docstrings(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef)):
            body = getattr(n, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def scan() -> list[tuple[str, int, str, str]]:
    """返回 `[(相对路径, 行号, 物种, 那一句)]`。"""
    hits: list[tuple[str, int, str, str]] = []
    for p in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)) or id(node) in docs:
                continue
            text = node.value.strip().replace("\n", " ")
            if len(text) < 8:
                continue
            kind = ""
            if CLAIMS.search(text):
                kind = "断言词"
            elif STRUCT.search(text):
                kind = "结构断言"
            if kind:
                hits.append((str(p.relative_to(ROOT)), node.lineno, kind, text[:120]))
    return hits


def main() -> int:
    hits = scan()
    # **未交代依据的排在最前面** —— 人的注意力该先花在那儿。
    def rank(item: tuple[str, int, str, str]) -> tuple[int, str, int]:
        text = item[3]
        if MEASURED.search(text):
            return (2, item[0], item[1])
        if HEDGED.search(text):
            return (1, item[0], item[1])
        return (0, item[0], item[1])

    hits.sort(key=rank)
    print(HEADER)
    n_bare = sum(1 for h in hits
                 if not MEASURED.search(h[3]) and not HEDGED.search(h[3]))
    print(f"\n共 {len(hits)} 处 · 其中**没交代依据**的 {n_bare} 处（排在前面）\n")
    for rel, line, kind, text in hits:
        if MEASURED.search(text):
            tag = "有实测记录"
        elif HEDGED.search(text):
            tag = "已限定"
        else:
            tag = "**没交代依据**"
        print(f"  [{kind}][{tag}] {rel}:{line}")
        print(f"      {text}\n")
    print("提示：改完再跑一次，确认清单里少的是该少的那几条。")
    return 0        # **永远返回 0** —— 这是清单，不是判决


if __name__ == "__main__":
    sys.exit(main())
