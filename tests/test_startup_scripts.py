"""启动脚本：给不懂命令行的人用的那条路。

盯住四件事，每件都有来由：

1. **不许替用户装东西**（`sudo` / `curl | sh` / `rm -rf`）—— 这个工具的基本承诺是
   "不改系统、不出网"，启动脚本自己先破坏它就荒唐了。
2. **缺 Python 要说清怎么办**（给官方下载页），而且要停住让人看得见 ——
   双击运行的窗口一关，错误信息就没人看见了。
3. **退出码要传下去**：双击的人看不见退出码，但"启动失败（退出码 N）"这句提示靠它。
4. **中文紧跟变量必须写 `${var}`** —— macOS 自带的是 bash 3.2，它会把中文的第一个字节
   当成变量名的一部分。真踩过：`echo "…$code）。"` 报 `code）。: unbound variable`。
"""

from __future__ import annotations

import re
import stat
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAC = ROOT / "start.command"
WIN = ROOT / "start-windows.bat"
README = ROOT / "README.md"

#: 变量后紧跟非 ASCII 字符（中文），且没有用 ${} 括起来
BAD_VAR_BEFORE_CJK = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F\s\"')\]},;:/]")


class TestStartScripts(unittest.TestCase):

    def test_both_platforms_have_one(self):
        self.assertTrue(MAC.exists(), "缺 macOS 的一键脚本")
        self.assertTrue(WIN.exists(), "缺 Windows 的一键脚本")

    def test_mac_script_is_double_clickable(self):
        text = MAC.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!"), "缺 shebang，双击不起作用")
        self.assertTrue(MAC.stat().st_mode & stat.S_IXUSR, "缺可执行位（双击会被拒）")
        self.assertIn("dirname", text, "要切到脚本自己的目录，双击才是从任何地方都能用")

    def test_neither_installs_anything_behind_your_back(self):
        for f in (MAC, WIN):
            text = f.read_text(encoding="utf-8")
            for bad in ("sudo ", "curl | sh", "curl -fsSL", "rm -rf", "pip install"):
                self.assertNotIn(bad, text, f"{f.name} 里有 {bad} —— 启动器不许替人装东西")

    def test_cjk_after_a_variable_is_braced(self):
        """macOS 的 bash 3.2 会把中文第一字节吃进变量名 —— 一律用 ${var}。"""
        text = MAC.read_text(encoding="utf-8")
        bad = [ln for ln in text.splitlines()
               if BAD_VAR_BEFORE_CJK.search(ln)
               and not ln.lstrip().startswith("#")]
        self.assertEqual(bad, [], f"这些行要改成 ${{var}}：{bad}")

    def test_tells_you_what_to_do_without_python(self):
        for f in (MAC, WIN):
            text = f.read_text(encoding="utf-8")
            self.assertIn("python.org/downloads", text, f"{f.name} 没给下载页")
            self.assertIn("3.10", text, f"{f.name} 没说清要哪个版本")

    def test_stops_so_you_can_read_the_error(self):
        self.assertIn("read -r", MAC.read_text(encoding="utf-8"))
        self.assertIn("pause", WIN.read_text(encoding="utf-8"))

    def test_exit_code_is_passed_through(self):
        mac = MAC.read_text(encoding="utf-8")
        self.assertRegex(mac, r"exit \$\{?code\}?", "macOS 脚本要把退出码传下去")
        self.assertIn("exit /b", WIN.read_text(encoding="utf-8"))

    def test_says_where_it_listens(self):
        for f in (MAC, WIN):
            self.assertIn("127.0.0.1:8765", f.read_text(encoding="utf-8"),
                          f"{f.name} 要写清服务只在本机")

    def test_readme_tells_both_paths(self):
        """README 与脚本体不能各说各话 —— 主路是 clone，双击是补充。"""
        text = README.read_text(encoding="utf-8")
        self.assertIn("git clone", text)
        self.assertIn("start.command", text)
        self.assertIn("start-windows.bat", text)


if __name__ == "__main__":
    unittest.main()
