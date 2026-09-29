"""仓库里不许出现非公开来源的材料身份（见 tools/check_discretion.py）。

这条检查是**踩过之后**补的：两份尽调材料的公司名跟着注释和提交信息上了公开仓库。
所以它自己也得有测试 —— 一个不会咬人的检查比没有检查更坏。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "check_discretion.py"


class TestDiscretion(unittest.TestCase):
    def test_tool_self_test_passes(self) -> None:
        """工具的自测：塞一个名字进去必须被抓到（否则它是摆设，不许留在仓库里）。"""
        r = subprocess.run([sys.executable, str(TOOL), "--self-test"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("不是摆设", r.stdout)

    def test_repo_is_clean(self) -> None:
        """真扫一遍：当前仓库（被跟踪的文件 + 最后一条提交信息）必须是干净的。"""
        r = subprocess.run([sys.executable, str(TOOL)],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0,
                         "仓库里出现了非公开来源的材料身份：\n" + r.stdout)

    def test_banned_list_is_not_plaintext(self) -> None:
        """★ 名单本身不许是明文 —— 这个文件也在公开仓库里。

        写这条的当时我就差点犯这个错：第一版把名字明文列在工具里，
        等于把要保护的名字**又公开了一遍**。
        """
        src = TOOL.read_text(encoding="utf-8")
        sys.path.insert(0, str(ROOT / "tools"))
        import check_discretion as cd          # noqa: PLC0415

        self.assertTrue(cd.banned(), "名单是空的 —— 那这检查没有任何作用")
        for name in cd.banned():
            self.assertNotIn(name, src,
                             f"名单里的「{name}」在工具源码里是明文 —— 它也在公开仓库里")


if __name__ == "__main__":
    unittest.main()
