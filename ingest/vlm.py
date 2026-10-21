"""用**本机视觉语言模型**判版面 —— 只回答「这页几栏、切在哪」，**绝不碰数字**。

## 为什么需要它（实测：一份 39 页盖章审计报告）

那份扫描件的资产负债表是 T 型（左边资产、右边负债）。macOS Vision 把两栏的
行**混成一行**，于是：

    负積和所有者权益（或股东 益） ‖ 负情合计 ‖ 所有着权益（成駁东权益）公计】
    遨动負微： ‖ 短期㑤改 ‖ 預收款項 ‖ 皮交税费 ‖ 申浇动灸微：

整页聚出 **22 个等距列心**、`_two_sided_cut` 返回 None —— 几何判据判不出双栏，
表就丢了（资产负债表、现金流量表都没定位到）。

**同一页交给本机 `qwen3.5:4b`**：它一眼说「左右两栏」，左栏 26 个科目名全对
（含 Vision 全毁掉的「流动资产合计」「非流动资产合计」）。

## 分工：它判版面，数字仍走确定性路径

**这不是「用大模型替掉 OCR」。** 大模型是生成式的 —— 它会漏、会编，
而数字错是**静默错**，正是本项目最防的一类。

所以这里**只让它回答一个几何问题**：切点在哪（占页宽的百分之几）。
答案还要过一道范围校验（0.25–0.75），不合就当作「不知道」。
**数字一个都不从它这里来。**

## 什么情况下才调它

每页 ~1–2 分钟（4B，CPU），**绝不能全本跑**。所以设了三道闸：

    ① 必须是走 OCR 的页（上游已经保证）
    ② 几何判据先试过、判不出（调用方保证）
    ③ 这一页**密布数字**（`_dense_with_numbers`）—— 正文页不问他

三道闸同时成立才会真的调模型。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

#: 本机 ollama。跟 `llm/backends.py` 用同一个约定。
HOST = os.environ.get("FREANALYST_OLLAMA", "http://127.0.0.1:11434")

#: 默认用哪个模型判版面。实测 `qwen3.5:4b`（3.4GB）在这份材料上判对了双栏、
#: 左栏 26 个科目名全对。**换模型必须先拿真材料量过**，别凭「听说更强」。
MODEL = os.environ.get("FREANALYST_VLM_MODEL", "qwen3.5:4b")

#: 总开关。
#:
#: **默认关。** 这一版没跑通 —— 实测数据看下面「为什么默认关」。
#: 打开用 `FREANALYST_VLM_LAYOUT=1`。
ENABLED = os.environ.get("FREANALYST_VLM_LAYOUT", "0") != "0"

# ── 为什么默认关（2026-10-21 实测，39 页盖章审计报告）────────────────
#
# 设想：几何判不出双栏时，让视觉模型给一个切点。
#
# 实测结果：**没修好，还慢了 6 倍**（19 秒 → 110 秒）。
#
# ① 提示词让它「表态」它就默认单栏 —— 同一页第 4 页（真 T 型双栏）：
#    问「单栏还是两栏」→ 答「单栏」✗
#    问「左右两半各印的什么」→ 答「分列资产与负债及所有者权益」✓
#    **让它描述它才看得见，让它表态它就偷懒。**
#
# ② 就算判对了「是两栏」，它也**给不出可靠的坐标**：
#    问「分界线在页宽百分之几」→ 答「单栏」✗
#
# ③ 而且这一页的几何根本没法切 —— 22 个列心**有 18 个被判成标签列**，
#    从 0.222 连续铺到 0.706、合成一组。行被打碎之后每个格子都长得像标签。
#    所以「模型判两栏 + 几何算切点」这条组合路也不成立 ✗
#
# 结论：**这个页型不是「换个引擎」能解决的**，得让模型做结构化转录
# （它确实读对了左栏 26 个科目名），但那属于另一件事 —— 数字仍须走 OCR。
#
# 留着这份代码是因为：`split_two_sided(cut_x=…)` 这条口子是干净的，
# 等哪天有了可校准的双栏判据（在**多份**材料上都对），直接接上就能用。

#: 渲染倍率。判版面不需要高倍率 —— 150 dpi 够看清栏线，而且比 200 dpi 快。
_DPI = 150

#: 切点必须落在这个区间里。太靠边的「切点」说明模型在胡说，
#: 宁可判「不知道」也不能乱切 —— 乱切会把合计行切走，勾稽从「平」变成「数据不足」。
_CUT_MIN, _CUT_MAX = 0.25, 0.75

_CACHE = Path(__file__).resolve().parent.parent / "cache" / "vlm"

PROMPT = (
    "这是一页扫描的财务报表。只回答一个问题：\n"
    "这页是**单栏**，还是**左右两栏**（左边一栏、右边一栏并排）？\n\n"
    "如果是左右两栏，回答两栏之间**分界线在页面宽度的百分之几**，"
    "只输出一个数字（例如 50）。\n"
    "如果是单栏，只输出两个字：单栏。\n\n"
    "不要解释，不要抄表里的内容，只输出数字或「单栏」。"
)


#: `available()` 的结果缓存 —— 否则每一页都要去敲一次 `/api/tags`。
#: 本进程内只探一次：模型装没装，跑起来之后不会变。
_AVAILABLE: bool | None = None


def available() -> bool:
    """本机有没有能判版面的视觉模型。**问不出来就当作没有** —— 不报错、不改行为。"""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    if not ENABLED:
        _AVAILABLE = False
        return False
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=3) as r:
            tags = json.loads(r.read())
        names = {m.get("name", "") for m in (tags.get("models") or [])}
        _AVAILABLE = any(n == MODEL or n.startswith(MODEL.split(":")[0])
                         for n in names)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        _AVAILABLE = False
    return _AVAILABLE


def _key(pdf: Path, page: int) -> str:
    """缓存键。

    ⚠️ **必须把提示词揉进去。** 只按文件+页码+模型算键的话，改了提示词
    还会命中旧缓存 —— 拿旧答案当新答案用，跟「迟到的成绩单」是同一类坑。
    """
    st = pdf.stat()
    ph = hashlib.sha1(PROMPT.encode("utf-8")).hexdigest()[:8]
    raw = f"{pdf.resolve()}:{st.st_mtime_ns}:{page}:{MODEL}:{_DPI}:{ph}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _render(pdf: Path, page: int) -> bytes | None:
    """把一页渲成 PNG 字节。没有 pypdfium2 就返回 None（不报错）。"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        doc = pdfium.PdfDocument(str(pdf))
        img = doc[page - 1].render(scale=_DPI / 72).to_pil()
        buf = __import__("io").BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:                                   # noqa: BLE001
        return None


def _ask(png: bytes) -> str:
    body = {
        "model": MODEL, "stream": False, "think": False,
        "messages": [{
            "role": "user", "content": PROMPT,
            "images": [base64.b64encode(png).decode("ascii")],
        }],
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        f"{HOST}/api/chat", method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return str(json.loads(r.read()).get("message", {}).get("content", ""))


def _parse(answer: str) -> float | None:
    """从回答里读出切点。**读不准就返回 None，绝不猜。**

    接受 `50` / `50%` / `50.5`；`单栏` 和任何含糊说法都当「不知道」（None）。
    """
    s = (answer or "").strip()
    if not s:
        return None
    if "单栏" in s:                       # 明确说单栏 → 不切
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if len(nums) != 1:
        # 0 个 = 没答；多个 = 它在解释/抄数字 —— 都不采信
        return None
    try:
        val = float(nums[0])
    except ValueError:
        return None
    if val < 25 or val > 75:              # 可能它答成了像素或别的单位
        return None
    return val / 100.0


def gutter(pdf: str | Path, page: int) -> float | None:
    """问本机模型：这一页的切点在页宽的哪个比例。``None`` = 别切 / 不知道。

    **只给几何，不给数字。** 上游拿它当 `cut_x` 用。
    """
    pdf = Path(pdf)
    if not pdf.exists() or page < 1:
        return None

    cache = _CACHE / _key(pdf, page) / "gutter.json"
    if cache.exists():
        try:
            got = json.loads(cache.read_text(encoding="utf-8"))
            v = got.get("cut")
            return float(v) if v is not None else None
        except (OSError, ValueError, TypeError):
            pass                                # 缓存坏了就重问，别装作没事

    png = _render(pdf, page)
    if png is None:
        return None
    try:
        answer = _ask(png)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    cut = _parse(answer)

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"cut": cut, "raw": answer[:200], "model": MODEL},
                       ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                                    # 写不进缓存不该让主流程失败
    return cut


def _is_dense_with_numbers(rows) -> bool:
    """这一页是不是**密布数字** —— 报表页会，正文页不会。

    用来当第三道闸：不密的话不花那 1–2 分钟去问模型。
    门槛取 10：正文页偶有金额（比如「注册资本 1000 万元」），报表页动辄几十个。
    """
    from . import ocr

    n = 0
    for row in rows:
        for cell in row:
            text = str(cell[-1] if isinstance(cell, (list, tuple)) else cell)
            if ocr.is_amount_ish(text):
                n += 1
    return n >= 10


def propose_cut(rows, pdf: str | Path, page: int) -> float | None:
    """三道闸都过了才问模型；否则 `None`（调用方保持原行为）。

    调用方已经确认过「几何判据判不出双栏」—— 这里只负责「要不要问」和「问什么」。
    """
    if not ENABLED or not rows:
        return None
    # 模型没装就别去连 —— 否则每页都白等一次超时
    if not available():
        return None
    if not _is_dense_with_numbers(rows):
        return None
    return gutter(pdf, page)
