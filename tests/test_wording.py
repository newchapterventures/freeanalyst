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


class TestPreciseWording(unittest.TestCase):

    def test_no_zero_egress_phrase_in_user_facing_files(self):
        for name in USER_FACING:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("零出境", text,
                             f"{name} 里还有「零出境」—— 会被读成「只承诺不跨境」")

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
