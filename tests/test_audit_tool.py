"""`tools/audit_wording.py` **本身**不该坏掉 —— 只验这一件事。

## 为什么只验这么点
它是**清单工具**，不是守卫：命中里大多数是合理的说法，
所以它**永远返回 0**（设计如此，防止"狼来了"）。

因此这里**只说三件事**：
  ① 能跑起来、退出码 0；
  ② 输出里有标题和计数；
  ③ 条目标了依据档（有实测记录 / 已限定 / 没交代依据）。

**故意不断言**「命中数应该等于几」「某个词必须出现」——
那会让它变成一道会喊狼来了的守卫，
而"一个会喊狼来了的守卫，比没有守卫更坏"（项目里已经栽过一次）。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "audit_wording.py"


class TestWordingAuditTool(unittest.TestCase):
    def test_runs_and_returns_zero(self):
        r = subprocess.run([sys.executable, str(TOOL)], capture_output=True,
                           text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, f"工具挂了：{r.stderr[-400:]}")

    def test_output_has_header_and_count(self):
        r = subprocess.run([sys.executable, str(TOOL)], capture_output=True,
                           text=True, timeout=120, cwd=str(ROOT))
        self.assertIn("待过目清单", r.stdout)
        self.assertIn("共", r.stdout)
        self.assertIn("处", r.stdout)

    def test_items_are_tagged_with_evidence_level(self):
        """每一条都要说清它凭什么 —— 否则人没法快速筛。"""
        r = subprocess.run([sys.executable, str(TOOL)], capture_output=True,
                           text=True, timeout=120, cwd=str(ROOT))
        if "共 0 处" in r.stdout:
            self.skipTest("清单为空，没有条目要标注")
        self.assertTrue(
            any(t in r.stdout for t in ("有实测记录", "已限定", "没交代依据")),
            "条目没有依据档位")


if __name__ == "__main__":
    unittest.main()
