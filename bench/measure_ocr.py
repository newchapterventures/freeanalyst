"""量 OCR 的**金额精度** —— 不许"我觉得好点了"。

判据不靠肉眼，靠 `parse_amount` 的状态统计（ok / repaired / suspect）加 Vision 的置信度。
**只打印计数与比例，不打印任何金额、不打印文件名。**

## 已经量出来的两条结论（2026-09-24，某 104 页扫描件）

1. **关掉语言纠正没有用** ✗ —— 第 7-8 页：开/关都是 `ok 100 / repaired 88 / suspect 4`
   （192 个金额格完全一致）；第 72-74 页：开/关都是 `35 / 48 / 0`。
   所以 `vision_ocr.swift` 里那个开关**默认仍为开**，不要凭感觉去关它。

2. **Vision 的自评置信度很弱** ✗ —— 192 个金额格里只有 14 个低于 0.5（7%），
   另一组只有 2 个（2%）。它抓不到"读得自信但读错"这种错。
   所以 `lowConf` 默认 0（关闭）：开着会喊狼来了，而**会误报的校验比没有校验更坏**。

## 真正的问题（同一个实测里的第三个数字）

**46%~58% 的金额要经过 `repair_systematic`** —— 修的是「逗号被认成句点或空格」，
判据严（分组必须恰好 3 位，不符合就不猜），所以修复大概率是对的。

真根因在别处：**数字被替换或截断**（`1,234,567,890.12` → `1,234,567.12`）——
形状完全合法，任何形状校验都抓不到，只能靠**页面内勾稽**（用报表自己的恒等式反证）
或逐格二次识别。这个脚本就是用来量这两条路各自救回多少的。

用法：
    python3 bench/measure_ocr.py <材料 PDF> [起始页 结束页]
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingest import ocr                                     # noqa: E402

SWIFT = ROOT / "ingest" / "vision_ocr.swift"


def run(pdf: str, first: int, last: int, *, correction: bool = True,
        low_conf: float = 0.0, scale: str = "3.0") -> dict | None:
    cmd = ["swift", str(SWIFT), pdf, str(first), str(last), scale,
           "1" if correction else "0", str(low_conf)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0:
        print("  OCR 失败：", p.stderr.strip()[:200])
        return None
    return json.loads(p.stdout)


def stats(payload: dict) -> tuple[Counter, list[float]]:
    cnt: Counter = Counter()
    confs: list[float] = []
    for page in payload.get("pages", []):
        for row in page.get("rows", []):
            for c in row.get("cells", []):
                t = str(c.get("t", ""))
                if not ocr.is_amount_ish(t):
                    continue
                _, status = ocr.parse_amount(t)
                cnt[status] += 1
                if c.get("c") is not None:
                    confs.append(float(c["c"]))
    return cnt, confs


def report(tag: str, cnt: Counter, confs: list[float]) -> None:
    total = sum(cnt.values()) or 1
    low = sum(1 for c in confs if c < 0.5)
    print(f"  {tag}：金额格 {total} · ok {cnt['ok']}（{cnt['ok'] / total:.0%}）· "
          f"repaired {cnt['repaired']}（{cnt['repaired'] / total:.0%}）· "
          f"suspect {cnt['suspect']} · 置信度<0.5 的 {low}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pdf = sys.argv[1]
    pairs = [(int(sys.argv[2]), int(sys.argv[3]))] if len(sys.argv) > 3 else [(7, 8), (72, 74)]
    for first, last in pairs:
        print(f"\n=== 第 {first}-{last} 页 ===")
        for tag, corr in (("语言纠正【开】", True), ("语言纠正【关】", False)):
            payload = run(pdf, first, last, correction=corr)
            if payload:
                report(tag, *stats(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
