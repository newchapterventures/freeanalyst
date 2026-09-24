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
DOC = ROOT / "webapp_doc.html"

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


def check_doc() -> None:
    """说明文件：六节必须齐全，闭源模型的数据风险必须写明。

    这六节是使用者点名要的 —— 少一节就是没写完，而"文件在不在"用眼睛看不出来。
    """
    if not DOC.exists():
        fail("缺 webapp_doc.html（说明文件页）")
        return
    src = DOC.read_text(encoding="utf-8")
    bilingual = ('class="lang-zh' in src) and ('class="lang-en' in src)
    print(f"  说明文件：{len(src)} 字节 · 中英双语块 {'✓' if bilingual else '✗'}")
    for topic in ("功能", "免责声明", "使用哪些模型", "使用方法", "如何调试",
                  "接入自己的大模型"):
        if topic not in src:
            fail(f"说明文件缺一节：{topic}")
    if "数据风险" not in src or "离开了本机" not in src:
        fail("说明文件没写清闭源模型的数据风险（这是点名要的一条）")
    if "算术绝不用模型" not in src:
        fail("说明文件没讲「算术绝不用模型」这条底线")
    if 'id="docLink"' not in PAGE.read_text(encoding="utf-8"):
        fail("向导页上没有说明文件入口（#docLink）")


def check_branding() -> None:
    """品牌与版本：标语在 logo/字标下方、署名在其下、版本号在底部。

    版本号必须**取自服务端**：页面自己写一个号，迟早会跟服务对不上，
    而对不上的那一刻没人知道该信哪个。
    """
    src = PAGE.read_text(encoding="utf-8")
    if 'class="tagline-row"' not in src:
        fail("标语没有放在 logo 与字标下方的整行里（.tagline-row）")
    if "by New Chapter Ventures" not in src:
        fail("缺署名 by New Chapter Ventures")
    # 署名必须**在字标块内、且是静态文字**，与字标一上一下夹住算盘
    seg = src.split('class="head-txt"', 1)[-1].split('class="doclink"', 1)[0]
    if '<div class="byline">by New Chapter Ventures</div>' not in seg:
        fail("署名不在字标块（.head-txt）内，或不是静态文字 —— "
             "中英相同的字不该依赖 JS 才渲染出来")
    rule = src.split(".head-txt{", 1)[-1].split("}", 1)[0]
    if "align-self:stretch" not in rule or "space-between" not in rule:
        fail("字标块没有撑满算盘高度（align-self:stretch + space-between）—— "
             "「字标贴算盘顶、署名贴算盘底」就是靠这两条实现的")
    # 三行左对齐：标语左缩进必须与算盘宽**同源**（算式），不能是写死的数字。
    # 这里曾经写死 padding-left:54px，后来算盘改成按高度缩放、宽度变 27.6px，
    # 标语就悄悄右移 10px —— 三行错开，而所有测试都是绿的。
    flat = "".join(src.split())
    if "--logo-w:calc(var(--logo-h)*19/33)" not in flat:
        fail("算盘宽度没有从高度算出来（--logo-w:calc(var(--logo-h) * 19 / 33)）")
    if "width:var(--logo-w)" not in flat:
        fail("算盘的 CSS 宽度没跟随 --logo-w")
    if "padding-left:calc(var(--logo-w)+var(--head-gap))" not in flat:
        fail("标语的左缩进不是 calc(--logo-w + --head-gap) —— "
             "左缩进一旦写死数字，改算盘尺寸时三行就会错开")
    if 'data-i18n="tagline">本地估值向导' not in src:
        fail("标语没有静态中文兜底（JS 没跑起来就整行是空的）")
    # 装饰性命令行那行与标语说的是同一件事，必须在同一列（左沿同源）
    row = src.split('class="tagline-row"', 1)[-1].split('<div class="pills"', 1)[0]
    if 'class="prompt"' not in row:
        fail("命令行那行不在 .tagline-row 里 —— 与标语同块、左沿同源")
    if re.search(r"\.prompt\{[^}]*padding-left", src):
        fail("命令行自己写了左缩进 —— 缩进只该有一处（.tagline-row），写两份早晚错开")
    # 兜底属性与 CSS 变量别各说各话
    mh = re.search(r"--logo-h:(\d+)px", src)
    ms = re.search(r'<svg class="logo" width="(\d+)" height="(\d+)"', src)
    if not mh or not ms:
        fail("找不到 --logo-h 或 svg 的兜底宽高")
    elif ms.group(2) != mh.group(1):
        fail(f"算盘兜底高度 {ms.group(2)} 与 --logo-h {mh.group(1)} 不一致")
    if 'id="ver"' not in src:
        fail("页面底部缺版本号（#ver）")
    if "VER = (h && h.version)" not in src:
        fail("版本号不是从 /api/health 取的（不许页面自己写死）")
    if 'id="ver"' not in DOC.read_text(encoding="utf-8"):
        fail("说明文件页底部缺版本号")


def check_script_syntax(script: str) -> None:
    """页面里的 JS 语法必须过一遍 `node --check`。

    为什么加这条：改词表时一次手滑留下了半行字符串 —— 页面直接白屏，
    而**所有测试都是绿的**（测试查的是"某个 token 在不在"，不是"能不能解析"）。
    node 不在就跳过并明说 —— 一个会喊狼来了的检查比没有检查更坏。
    """
    import shutil
    import subprocess
    import tempfile

    exe = shutil.which("node")
    if not exe:
        print("  （没装 node，**这一项没验**）")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(script)
        tmp = Path(f.name)
    try:
        r = subprocess.run([exe, "--check", str(tmp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            fail("页面 JS 语法不通过（页面会白屏）：\n" + (r.stderr or "").strip()[:400])
    finally:
        tmp.unlink(missing_ok=True)
    print("  JS 语法 ✓")


def check_percent_fields() -> None:
    """百分比字段：**% 显示在框里，人只填数值**；换算只在引擎侧一处。

    为什么值得一条断言：引擎只认"带单位的字符串"。人填个光秃秃的 `8.5`
    会被读成 850% —— 100 倍级的静默错误，报告照样出得来。
    界面上把 % 显示出来，是为了让人不必自己换算；换算只允许引擎那一处做，
    否则界面 / 服务端 / 命令行就有三份算术，早晚不一致。
    """
    src = PAGE.read_text(encoding="utf-8")
    style = src.split("<style>", 1)[-1].split("</style>", 1)[0]
    if ".pctsuf" not in style or 'class="pctsuf"' not in src:
        fail("百分比字段没有把 % 显示在框里（.pctsuf）")
    if 'q.unit === "%"' not in src:
        fail("界面没按引擎给的 unit 判断哪些字段是百分比 —— 会漏标或错标")
    if '"unit.pct"' not in src:
        fail('缺少「只填数值（填 8.5 即 8.5%）」的提示语 unit.pct')
    eng = (ROOT / "intake.py").read_text(encoding="utf-8")
    if 'unit="%"' not in eng or "def percent_keys" not in eng:
        fail("引擎侧没有把字段标成 unit='%' —— 界面无从判断哪些是百分比")
    if "def as_percent_text" not in eng or "def normalize_percents" not in eng:
        fail("引擎侧缺 as_percent_text / normalize_percents（补 % 的那一步）")
    if "8.5%" not in eng:
        fail("引擎侧没写清「8.5 表示 8.5%」—— 这条规则必须写在代码旁")


def check_origins() -> None:
    """来源类别：引擎里定义的每一类，页面上都要有对应标签。

    引擎加了第五类、页面不认 → 那一类的标签**静默消失**，
    人就分不清"报表里有的"和"公司之外要我自己找的"。
    """
    src = PAGE.read_text(encoding="utf-8")
    eng = (ROOT / "intake.py").read_text(encoding="utf-8")
    kinds = re.findall(r'^ORIGIN_[A-Z]+ = "([^"]+)"', eng, re.M)
    if len(kinds) != 4:
        fail(f"引擎里的来源类别不是 4 类：{kinds}")
    for k in kinds:
        if f'"{k}"' not in src:
            fail(f"页面上没有「{k}」这一类的标签 —— 界面上会静默不显示")
    if "originTag(q.origin)" not in src:
        fail("页面没有渲染来源标签（originTag）")
    print(f"  来源类别：{' / '.join(kinds)} · 页面均已识别 ✓")


def main() -> int:
    print(f"体检：{PAGE.name} 与 {LOGO.name} 与 {DOC.name}\n")
    src = PAGE.read_text(encoding="utf-8")
    html, script = split_page(src)
    print("── 页面元素 ──")
    check_ids(html, script)
    print("── JS 语法 ──")
    check_script_syntax(script)
    print("── 中英词表 ──")
    check_i18n(html, script, src)
    print("── 服务契约 ──")
    check_service_contract(src)
    print("── 品牌与版本 ──")
    check_branding()
    print("── 百分比字段 ──")
    check_percent_fields()
    print("── 来源类别 ──")
    check_origins()
    print("── logo 几何 ──")
    check_logo(LOGO)
    print("── 说明文件 ──")
    check_doc()
    print()
    if fails:
        print(f"不通过：{len(fails)} 项 ✗")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
