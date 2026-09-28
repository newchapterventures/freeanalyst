#!/usr/bin/env python3
"""核对对话面板的别名表 —— **每个别名都得能落在真实问题清单的标签上**。

    python3 tools/check_ask_aliases.py

## 为什么要有这个工具（它守的是一个真出过的错）
`webapp._ASK_ALIASES` 里原本写的是**猜的键名**（`da_ratio`），而真实清单里是
`da_pct_revenue` —— 键名一漂，「折旧摊销 12%」就**永远匹配不上**，用户只会看到
"认不出这是哪一项"。**材料里量出来的，不是想出来的。**

所以这条检查的意义是：**清单改了（加项、改名）之后，别名表会不会悄悄失效**。
坏掉的别名不是无害的 —— 它让人以为"这么说就能识别"。

## 永远不会返回非零（和 `audit_wording.py` 同一套道理）
它列的是**待看的清单**，不是判决：某个别名落不上，可能是有意为之（比如口语说法
本来就该让用户换词）。但**落不上又没写明理由的**，就该删掉或改成对得上的片段。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEADER = """别名核对 —— 每个别名都要能落在**真实问题清单**的标签上。

⚠️ 用正则抓的是**单行** `Q("键", "标签"` 写法 —— 多行写法的条目会漏掉，
   所以下面的"落不上"只代表**在抓到的这些里**没落上。"""


def main() -> int:
    import webapp  # noqa: PLC0415

    src = (ROOT / "intake.py").read_text(encoding="utf-8")
    pairs = re.findall(r'Q\(\s*"([a-z_0-9]+)"\s*,\s*"([^"]+)"', src)

    print(HEADER)
    print(f"\n清单里抓到 {len(pairs)} 条（键 · 标签）\n")

    ok, bad = [], []
    for word, frag in webapp._ASK_ALIASES:
        matched = [(k, l) for k, l in pairs if frag in l or frag.lower() in l.lower()]
        (ok if matched else bad).append((word, frag, matched))

    print("★ 能落上的：")
    for word, frag, matched in ok:
        names = " / ".join(l[:22] for _, l in matched[:3])
        more = f" …共 {len(matched)} 项" if len(matched) > 3 else ""
        print(f"   「{word}」→「{frag}」→ {names}{more}")

    if bad:
        print("\n⚠ **落不上的**（用户这么说就会「认不出」）：")
        for word, frag, _ in bad:
            print(f"   「{word}」→「{frag}」")
        print("   处理：要么改成对得上的片段，要么删掉 —— **别留着骗人**。")

    print(f"\n小结：{len(ok)}/{len(ok) + len(bad)} 个能落在真实标签上")
    return 0        # 永远 0：这是清单，不是判决


if __name__ == "__main__":
    sys.exit(main())
