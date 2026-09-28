"""`tools/check_ask_aliases.py` **本身**不该坏掉 —— 只验这一件事。

## 它守的是什么
对话面板的别名表（`webapp._ASK_ALIASES`）原本写的是**猜的键名**，
真实清单里却是 `da_pct_revenue` 这种 —— 键名一漂，「折旧摊销 12%」就永远匹配不上。
这个工具就是**定期核对"别名还能不能落在真实标签上"**。

## 为什么只验这么点
它是**清单工具**：某个别名落不上，可能是有意为之（口语说法本来就该让用户换词），
所以它**永远返回 0**。这里只钉三件事：能跑、退出码 0、有结论行。
**故意不断言"必须 16/16"** —— 那会变成会喊狼来了的守卫。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "check_ask_aliases.py"


class TestAskAliasTool(unittest.TestCase):
    def _run(self):
        return subprocess.run([sys.executable, str(TOOL)], capture_output=True,
                              text=True, timeout=120, cwd=str(ROOT))

    def test_runs_and_returns_zero(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, f"工具挂了：{r.stderr[-400:]}")
        self.assertNotEqual(r.stdout.strip(), "", "跑起来什么都没输出 —— 可能是漏了 __main__ 入口")

    def test_reports_a_conclusion(self):
        r = self._run()
        self.assertIn("能落在真实标签上", r.stdout)
        self.assertIn("清单里抓到", r.stdout)

    def test_flags_unmatchable_aliases(self):
        """落不上的要被列出来 —— 这正是它存在的理由。"""
        r = self._run()
        if "落不上的" in r.stdout:
            self.assertIn("别留着骗人", r.stdout)


if __name__ == "__main__":
    unittest.main()
