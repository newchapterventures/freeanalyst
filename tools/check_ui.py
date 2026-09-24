#!/usr/bin/env python3
"""一条命令体检界面：页面、词表、logo 几何。

    python3 tools/check_ui.py

为什么要有这个（三个都是**真踩过的坑**，所以变成能一键重跑的断言）：

1. **JS 引用了不存在的 id** —— 浏览器里只表现为"某个按钮没反应"，肉眼很难看出来。
2. **词表缺一条译文** —— 切到英文就多一块空白。中英必须一一对应。
3. **logo 几何错了** —— 珠子横向连成一条、上下档没有间隔、红色方块位置不对，
   这些在源码里都"看起来对"，只有算出来才知道。

另外顺手查两件与纪律有关的事：界面文案里不许出现口语（丢材料/认材料/出报告），
以及**服务与页面要对得上表**（`/api/health` 自报接口清单，页面据此提示"服务是旧版本"）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "webapp_page.html"
LOGO = ROOT / "logo.svg"

# 页面元素的规格（与 webapp_page.html 里的常量一致）
BW, BH = 3, 3                 # 方块大小
COLLOQUIAL = ["丢材料", "认材料", "出报告", "认出来了", "没认出来"]
#: 运行时才注入的 id（JS 生成），静态文件里查不到是正常的
DYNAMIC_IDS = {"unit"}
#: SVG defs / 内部用的 id，不算 DOM 元素
SVG_IDS = {"bg", "bs", "ba", "cbg", "cbs", "cba", "suanpan"}

fails: list[str] = []


def fail(msg: str) -> None:
    fails.append(msg)
    print("  ✗ " + msg)


def split_page(src: str) -> tuple[str, str]:
    """把页面切成 HTML 与 JS 两半（注释剥掉，注释里可以举反例）。"""
    html, _, rest = src.partition("<script>")
    script, _, after = rest.partition("</script>")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    return html + after, script


def check_ids(html: str, script: str) -> None:
    ids = set(re.findall(r'id="([\w-]+)"', html))
    used = set(re.findall(r'\$\("([\w-]+)"\)', script)) | \
        set(re.findall(r'getElementById\("([\w-]+)"\)', script))
    missing = sorted(used - ids - DYNAMIC_IDS - SVG_IDS)
    print(f"  元素 id：HTML {len(ids)} 个，JS 用到 {len(used)} 个")
    if missing:
        fail(f"JS 用到但 HTML 里没有的 id：{missing}")
    for n in range(1, 7):
        if f'id="s{n}"' not in html:
            fail(f"缺第 {n} 步的容器 id=s{n}")


def check_i18n(html: str, script: str, src: str) -> None:
    used = set(re.findall(r'data-i18n(?:-html|-ph)?="([^"]+)"', html))
    # 词边界：closest(".card")、split("\n") 里也有 t(" ，不加边界会误报（真误报过）。
    used |= set(re.findall(r'(?<![\w.])t\("([^"]+)"\)', script))
    zh = set(re.findall(r'"([\w.]+)":', src.split("zh: {", 1)[1].split("\n  },", 1)[0]))
    en = set(re.findall(r'"([\w.]+)":', src.split("en: {", 1)[1].split("\n  }", 1)[0]))
    print(f"  词表：中文 {len(zh)} 条 · 英文 {len(en)} 条 · 用到 {len(used)} 个 key")
    for label, missing in (("中文", used - zh), ("英文", used - en)):
        if missing:
            fail(f"{label}词表缺：{sorted(missing)}")
    if zh != en:
        fail(f"中英不一致：只有中文 {sorted(zh - en)}，只有英文 {sorted(en - zh)}")
    for w in COLLOQUIAL:
        if w in html:
            fail(f"界面文案里出现口语：{w}")


def check_service_contract(src: str) -> None:
    """页面要在「服务是旧进程」时说清楚 —— 实测点按钮只回 unknown endpoint 过。"""
    if 'id="stale"' not in src:
        fail("页面缺少「服务是旧版本」提示条（#stale）")
    if "endpoints" not in src:
        fail("页面没有跟服务对表（/api/health 的 endpoints）")


def check_logo(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    tok = re.compile(r'<g class="([^"]+)"|<use\b([^>]*?)/?>|<rect\b([^>]*?)/?>', re.S)
    attr = re.compile(r'([\w-]+)="([^"]*)"')
    cls, beads, rods, eyes = "", [], [], []
    for g, use, rect in tok.findall(src):
        if g:
            cls = g
            continue
        a = dict(attr.findall(rect or use))
        own = a.get("class", "")
        if use:
            beads.append(dict(x=float(a.get("x", 0)), y=float(a.get("y", 0)),
                              cls=own or cls, fill=""))
            continue
        w, h = float(a.get("width", 0)), float(a.get("height", 0))
        x, y = float(a.get("x", 0)), float(a.get("y", 0))
        if (w, h) == (BW, BH):
            if (x, y) == (0, 0):
                continue
            beads.append(dict(x=x, y=y, cls=own or cls, fill=a.get("fill", "").upper()))
        elif w == 1 and h >= 12:
            rods.append((x, y))
        elif w == 1 and h == 2:
            eyes.append((x, y))

    print(f"  logo：方块 {len(beads)} 个 · 轴 {len(rods)} 根 · 空档点 {len(eyes)} 个"
          f"（{path.name}）")
    if len(beads) != 28:
        fail(f"logo 方块 {len(beads)} 个，应 28（4 列 × 上 2 下 5）")
    if rods:
        fail(f"logo 不该有纵轴，却有 {len(rods)} 根")
    if eyes:
        fail(f"logo 不该有点，却有 {len(eyes)} 个")

    rows: dict[float, list] = {}
    for b in beads:
        rows.setdefault(b["y"], []).append(b)
    for y in sorted(rows):
        row = sorted(rows[y], key=lambda r: r["x"])
        gaps = [round(row[i + 1]["x"] - (row[i]["x"] + BW), 2) for i in range(len(row) - 1)]
        if any(g < 1 for g in gaps):
            fail(f"logo y={y} 这一排横向粘住了（缝 {gaps}）—— 轴距必须大于珠宽")
        if len(row) != 4:
            fail(f"logo y={y} 有 {len(row)} 个方块，每排应 4 个")
    ys = sorted(rows)
    upper = [y for y in ys if y < 9]
    lower = [y for y in ys if y > 9]
    if len(upper) != 2 or len(lower) != 5:
        fail(f"logo 上档 {len(upper)} 排、下档 {len(lower)} 排，应 2 和 5")
    if upper and lower:
        gap = lower[0] - (upper[-1] + BH)
        print(f"  上下档间隔：{gap} 格")
        if gap < 3:
            fail(f"logo 上下档间隔只有 {gap} 格，太窄（要 ≥3）")
    acc = [b for b in beads if "acc" in b["cls"] or b["fill"] == "#E5484D"]
    xs = sorted({b["x"] for b in beads})
    want = (xs[0], upper[1]) if xs and len(upper) > 1 else None
    if len(acc) != 1:
        fail(f"logo 红色方块应只有 1 个，实际 {len(acc)} 个")
    elif (acc[0]["x"], acc[0]["y"]) != want:
        fail(f"logo 红块位置 {(acc[0]['x'], acc[0]['y'])}，应为左 1 上 2 = {want}")


def main() -> int:
    print(f"体检：{PAGE.name} 与 {LOGO.name}\n")
    src = PAGE.read_text(encoding="utf-8")
    html, script = split_page(src)
    print("── 页面元素 ──")
    check_ids(html, script)
    print("── 中英词表 ──")
    check_i18n(html, script, src)
    print("── 服务契约 ──")
    check_service_contract(src)
    print("── logo 几何 ──")
    check_logo(LOGO)
    print()
    if fails:
        print(f"不通过：{len(fails)} 项 ✗")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
