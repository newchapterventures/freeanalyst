"""用词准确：说「材料不出本机」，不说「零出境」。

为什么单独一个测试：**「数据出境」在法律上是"跨境传输"**（PIPL 那套）。
用「零出境」描述这个工具会让人以为承诺是"只保证不传出国"，
而它实际做的是更严的那件事 —— **材料连这台电脑都不出**。

对一个要给 PE/VC 用、还会被 LP 的合规看的东西，这句话说错是实打实的问题。
这个测试挡住"以后又有人顺手写回零出境"。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 使用者看得到的文件
USER_FACING = ("webapp_page.html", "webapp_doc.html", "webapp_config.html",
               "README.md", "CHANGELOG.md")

#: 这份测试自己（和它的姊妹测试）要**引述**这条规矩，所以它们里出现「零出境」
#: 是在说"不许这么写"，不是自己写错。除了它们，**全仓库都不许出现**。
RULE_FILES = ("tests/test_wording.py", "tests/test_egress_guard.py")

#: 除源码以外还要扫的文本类型（文档也是给人看的 —— 架构图会拿给合规看）
SCAN_SUFFIX = (".md", ".html", ".py", ".swift", ".sh", ".command", ".txt", ".toml")
SKIP_DIRS = {".git", "__pycache__", "cache", "out", "node_modules", ".venv"}


def _scanned_files() -> list[Path]:
    """**全仓库**里人写的东西 —— 不只是那 5 个"使用者看得到"的文件。

    ★ 为什么补上这一步（实测踩到）：原来的覆盖面只有 5 个文件，
    于是 `docs/架构图.html`（"默认零出境"）和几份设计文档里的旧措辞**一直漂着没人管** ——
    规矩钉住了，钉子只钉了一个角。
    """
    out: list[Path] = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SCAN_SUFFIX:
            continue
        if SKIP_DIRS & set(p.relative_to(ROOT).parts):
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel in RULE_FILES:
            continue
        out.append(p)
    return out


class TestPreciseWording(unittest.TestCase):

    def test_no_zero_egress_phrase_in_user_facing_files(self):
        for name in USER_FACING:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("零出境", text,
                             f"{name} 里还有「零出境」—— 会被读成「只承诺不跨境」")

    def test_no_zero_egress_phrase_anywhere_in_the_repo(self):
        """★ 全仓库扫 —— 这条是为了堵住"规矩钉住了、钉子只钉了一个角"。

        实测：补这一步之前，`docs/架构图.html` 上明明写着「默认零出境」，
        `docs/进化路线.md`、`docs/valuation-spec.md`、`net.py` 的注释里也都有，
        而测试全绿 —— 因为覆盖面只有 5 个文件。
        """
        bad: list[str] = []
        for p in _scanned_files():
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "零出境" in text or "数据不出境" in text or "文件内容永不出境" in text:
                bad.append(p.relative_to(ROOT).as_posix())
        self.assertEqual(bad, [], f"这些文件里还有「零出境 / 数据不出境」：{bad}")

    def test_the_rule_itself_is_documented_somewhere(self):
        """规矩要留着出处 —— 不然下一个人不知道为什么不能这么写。"""
        me = (ROOT / "tests" / "test_wording.py").read_text(encoding="utf-8")
        self.assertIn("零出境", me)          # 测试自己引述规矩 = 允许
        self.assertIn("材料不出本机", me)

    def test_says_what_it_actually_means(self):
        page = (ROOT / "webapp_page.html").read_text(encoding="utf-8")
        self.assertIn("材料不出本机", page)
        self.assertIn("leaves this machine", page)
        # 界面上的标识必须把"连局域网都不去"这句说全
        self.assertIn("连局域网都不去", page)
        self.assertIn("not even the local network", page)

    def test_doc_page_draws_the_distinction(self):
        """说明文件里要点明与法律术语的区别 —— 这是给合规看的人准备的。"""
        doc = (ROOT / "webapp_doc.html").read_text(encoding="utf-8")
        self.assertIn("数据出境", doc)
        self.assertIn("跨境传输", doc)
        self.assertIn("cross-border data transfer", doc)
        for token in ("材料不出本机", "leaves this machine"):
            self.assertIn(token, doc, token)

    def test_readme_explains_the_term(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("机密材料不出本机", readme)
        # 换词的理由要写出来，不然下一个人又改回去
        self.assertIn("跨境传输", readme)


if __name__ == "__main__":
    unittest.main()
