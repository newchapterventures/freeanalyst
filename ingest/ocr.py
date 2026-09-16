"""OCR 层 —— 用 macOS 内置的 Vision 框架。

## 为什么选它

| | |
|---|---|
| 费用 | 免费 |
| 网络 | **零出境**（纯本地，无模型下载）—— 符合本项目的保密要求 |
| 语言 | 原生支持 **zh-Hans / zh-Hant**，英文 |
| 速度 | 约 1 秒/页 |
| 代价 | **只能在 macOS 上跑**（Vision 是苹果框架） |

Linux 上的人走 SEC EDGAR / 有文字层的 PDF 那条路，不影响。

## 两件必须做对的事

### 1. 坐标配对（`vision_ocr.swift` 里做）

Vision 把表格的**左列（科目名）和右列（金额）返回成两个独立文本块**。
直接顺序打印会变成「先全部科目名、再全部金额」—— 完全没法用。

实测：某资产负债表 105 个文本块，顺序输出是 1-76 行标签、77-133 行数字。
所以 Swift 那边按 y 坐标聚成行、行内按 x 排序，还原真正的表格结构。

### 2. 金额格式校验（本模块 `parse_amount`）

OCR 会认错标点。实测抓到的真例子：

    原文 334,719.50  →  OCR 认成 334.719.50

如果不校验，`_to_number` 会把它剥成 `33471950` —— **差 100 倍，而且不报错**。
财务数据上这类静默错误比明显报错危险得多。
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
from pathlib import Path

_SWIFT_SRC = Path(__file__).with_name("vision_ocr.swift")
_CACHE_ROOT = Path(__file__).resolve().parent.parent / "cache" / "ocr"

#: 严格的金额写法。**OCR 出来的数字必须完全符合它才认。**
#:
#:     6,840,705.08   ✓
#:     -14,439,051.25 ✓
#:     (601,579)      ✓
#:     186            ✓
#:     334.719.50     ✗  ← 逗号被认成句点，两个小数点
#:     1,079,71       ✗  ← 被空格断开
#:     505o18914969   ✗  ← 混进了字母 o
_STRICT_AMOUNT = re.compile(r"^\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?$")

#: 破折号表示零 / 无
_DASH = {"—", "–", "-", "－", "—", "N/A", "n/a", "不适用"}

#: 系统性 OCR 错误一：千分位逗号被认成句点。
#:
#:     334,719.50  →  334.719.50
#:     1,609,705.35 →  1.609.705.35
#:
#: **判据必须严格**：每个分组恰好 3 位，末组（如果有）恰好 2 位小数。
#: 这样 `45.508.51239`（末组 5 位）不会被误改 —— 那种错法归不了位，
#: 宁可当可疑值剔掉。
_PERIOD_GROUPED = re.compile(r"^(\d{1,3})((?:\.\d{3})+)(?:\.(\d{2}))?$")

#: 系统性 OCR 错误二：千分位逗号被认成空格。
#:
#:     85,748,813.69  →  85 748 813 69
_SPACE_GROUPED = re.compile(r"^(\d{1,3})((?:\s\d{3})+)(?:\s(\d{2}))?$")

#: 系统性 OCR 错误三：**千分位逗号后面多插了一个空格**。
#:
#:     7, 756,942, 510.86  →  7,756,942,510.86
#:     770, 501, 087. 14   →  770,501,087.14
#:     2, 562, 165,433.39  →  2,562,165,433.39
#:
#: 实测某上市公司 2022 审计报告（104 页扫描件）：40 个金额全栽在这一条上，
#: 包括 `资产总计` —— 导致整条勾稽判不了。
#:
#: **安全前提**：去掉空格后必须完全符合严格的三位分组，否则不修。
#: 这样 `1,234 5,678` 这种「本来是两格」的情况不会被误并
#: （去空格得 `1,2345,678`，分组不合法）。
#: 另要求原串含 `.` 或 `,` —— 否则 `1 2` 会被并成 `12`。
_SPACED_AMOUNT = re.compile(r"^[（(]?-?[\d\s,.]+[)）]?$")


def repair_systematic(text: str) -> str | None:
    """修**系统性** OCR 错误（逗号被认成句点或空格）。

    返回修好的字符串；判据不明确时返回 None —— **不猜**。

    为什么敢修：这不是随机噪声，是同一个字符被稳定认错，错误模型
    是确定的（`,` → `.` 或 ` `）。判据也严（分组必须恰好 3 位），
    修完还能用勾稽校验反证。随机错误才不能用这招。
    """
    s = text.strip()
    m = _PERIOD_GROUPED.match(s)
    if m:
        head, groups, cents = m.groups()
        out = head + groups.replace(".", ",")
        return f"{out}.{cents}" if cents else out
    m = _SPACE_GROUPED.match(s)
    if m:
        head, groups, cents = m.groups()
        out = head + groups.replace(" ", ",")
        return f"{out}.{cents}" if cents else out

    # 千分位逗号后多插了空格 —— 去掉所有空格再看分组合不合法
    if ("," in s or "." in s) and _SPACED_AMOUNT.match(s):
        t = re.sub(r"\s+", "", s)
        # **必须真的改变了字符串才算修复。**
        # 否则本来就合法的 `6,840,705.08` 也会被报成 "repaired"，
        # 让下游以为这个数被动过。
        if t != s and _STRICT_AMOUNT.match(t):
            return t
    return None


def available() -> bool:
    """这台机器能不能跑 OCR。"""
    return platform.system() == "Darwin" and shutil.which("swift") is not None


def why_unavailable() -> str:
    if platform.system() != "Darwin":
        return ("OCR 走的是 macOS 内置的 Vision 框架，当前系统是 "
                f"{platform.system()}，用不了。")
    if shutil.which("swift") is None:
        return ("找不到 swift 命令。装一下 Xcode Command Line Tools："
                "`xcode-select --install`。")
    return ""


# --------------------------------------------------------------------------
# 金额校验
# --------------------------------------------------------------------------

def parse_amount(text: str | None) -> tuple[float | None, str]:
    """解析一个金额单元格。返回 (数值, 状态)。

    状态是 `ok` / `dash`（破折号，表示零）/ `empty` / `suspect`（**格式不对**）。

    **格式不对的一律返回 None 并标 suspect，绝不猜。**
    实测踩到：`334,719.50` 被 OCR 认成 `334.719.50`，若按「剥掉非数字」
    处理会变成 33471950 —— 差 100 倍且不报错。财务数据上这种静默错误
    比明确报错危险得多。
    """
    if text is None:
        return None, "empty"
    s = text.strip().lstrip("¥$￥").strip()
    if not s:
        return None, "empty"
    if s in _DASH:
        return None, "dash"
    if not _STRICT_AMOUNT.match(s):
        fixed = repair_systematic(s)
        if fixed is not None and _STRICT_AMOUNT.match(fixed):
            # 系统性错误，已按明确判据修好。**记为 repaired**，
            # 让下游能单独统计「有多少数是修出来的」。
            neg = fixed.startswith("(")
            try:
                v = float(fixed.strip("()").replace(",", ""))
            except ValueError:
                return None, "suspect"
            return (-v if neg else v), "repaired"
        return None, "suspect"

    neg = s.startswith("(") and s.endswith(")")
    core = s.strip("()").replace(",", "")
    try:
        v = float(core)
    except ValueError:
        return None, "suspect"
    return (-v if neg else v), "ok"


def is_amount_ish(text: str) -> bool:
    """这一格看着像想写一个金额吗（哪怕写错了）。

    用来决定「格式不对」值不值得报警 —— 一个纯文字单元格不算金额错误。
    """
    s = (text or "").strip()
    if not s:
        return False
    # 全角标点也算 —— OCR 会把 `202,275,237.27` 认成 `202.，275,237.27`
    # （混进一个全角逗号）。不认全角的话这一格会被当成普通文字，
    # 「格式不对」就报不出来了。
    return bool(re.search(r"\d", s)) and bool(
        re.fullmatch(r"[\d\s.,()\-–—¥$￥%，、．（）]+", s))


# --------------------------------------------------------------------------
# 调 Swift
# --------------------------------------------------------------------------

def _cache_dir(pdf: Path) -> Path:
    key = hashlib.sha1(
        f"{pdf.resolve()}:{pdf.stat().st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    return _CACHE_ROOT / key


def ocr_pages(pdf: str | Path, first: int = 1, last: int = 0,
              timeout: int = 1800) -> dict[int, list[list[tuple[float, str]]]]:
    """对 PDF 的若干页做 OCR，返回 {页码: [[(x, 文本), ...], ...]}。

    **一次进程跑完整段**，不是每页 spawn 一次 —— 省掉每页约 1 秒的启动开销。
    结果按「文件路径 + mtime」缓存，同一份材料第二次读不重跑。

    ## 缓存只能「全中」才算命中（实测踩到）

    第一版是「缓存里有东西就直接返回」。于是先单独 OCR 过第 4、5 页之后，
    再请求 1–34 页会**只拿到那 2 页**，其余 32 页静默丢失 ——
    表现为「34 页的扫描件只 OCR 了 2 页」，而且不报错。

    所以现在**逐页核对**：缓存里缺哪几页就补跑哪几页，缺的部分才去调 OCR。
    """
    pdf = Path(pdf)
    if not available():
        raise RuntimeError(why_unavailable())
    if not pdf.exists():
        raise FileNotFoundError(f"找不到文件：{pdf}")

    cache = _cache_dir(pdf)
    cached = _read_cache(cache) or {}

    lo = max(1, first)
    hi = last if last > 0 else max(cached or {1: []})
    if hi < lo:
        return {}

    need = [n for n in range(lo, hi + 1) if n not in cached]
    if need:
        for start, end in _runs(need):
            cached.update(_run_swift(pdf, start, end, timeout))
        _write_cache(cache, cached)

    return {n: rows for n, rows in cached.items() if lo <= n <= hi}


def _runs(nums: list[int]) -> list[tuple[int, int]]:
    """把页码并成连续区间 —— 少启动几次 swift。"""
    if not nums:
        return []
    out: list[tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append((start, prev))
        start = prev = n
    out.append((start, prev))
    return out


def _run_swift(pdf: Path, first: int, last: int,
               timeout: int) -> dict[int, list[list[tuple[float, str]]]]:
    cmd = ["swift", str(_SWIFT_SRC), str(pdf), str(first), str(last)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"OCR 失败（swift 退出 {proc.returncode}）：{proc.stderr.strip()[:300]}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"OCR 输出不是合法 JSON：{e}") from e

    out: dict[int, list[list[tuple[float, float | None, str]]]] = {}
    for page in payload.get("pages", []):
        rows = []
        for r in page.get("rows", []):
            rows.append([(float(c.get("x", 0.0)), c.get("y"),
                          str(c.get("t", ""))) for c in r.get("cells", [])])
        out[int(page["page"])] = rows
    return out


def _read_cache(cache: Path):
    """读整个缓存目录。**不做区间过滤** —— 由 `ocr_pages` 逐页核对。"""
    if not cache.is_dir():
        return None
    out: dict[int, list[list[tuple[float, str]]]] = {}
    for f in cache.glob("*.json"):
        try:
            n = int(f.stem)
        except ValueError:
            continue
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            for row in raw:
                if row and len(row[0]) < 3:
                    return None      # 旧格式（没有 y 坐标）→ 重跑
            out[n] = [[(float(c[0]), c[1], str(c[2])) for c in row] for row in raw]
        except (OSError, json.JSONDecodeError, TypeError, ValueError, IndexError):
            return None          # 缓存坏了就重跑，别装作没事
    return out or None


def _write_cache(cache: Path, pages: dict[int, list[list[tuple[float, str]]]]) -> None:
    try:
        cache.mkdir(parents=True, exist_ok=True)
        for n, rows in pages.items():
            (cache / f"{n}.json").write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                     # 缓存写不进去不该让主流程失败


# --------------------------------------------------------------------------
# 列还原 —— 这一步做不对，金额就会落到错误的栏
# --------------------------------------------------------------------------

#: 同一个列的 x 容差（归一化坐标，1.0 = 页宽）。
_COL_TOL = 0.02

#: 「行次 / 附注」列里那些小整数的上限。中文财报的行次不会超过这个数。
_MAX_ROW_NUMBER = 400


def _is_row_number(text: str) -> bool:
    """像不像「行次 / 附注号」而不是金额。

    **这类小整数绝不能当金额。** 中文财报标准格式的表头是
    `项目 | 行次 | 期末余额 | 上年年末余额`，行次是 1、2、3……。
    """
    s = text.strip()
    if not re.fullmatch(r"\d{1,3}", s):
        return False
    return int(s) <= _MAX_ROW_NUMBER


def cluster_columns(rows: list[list[tuple[float, str]]]) -> list[float]:
    """把整页所有单元格的 x 起点聚成若干列，返回各列的中心 x（升序）。

    ## 为什么必须按坐标分列（实测踩到）

    只按「数字出现的先后顺序」猜列是不行的：OCR 有时认出「行次」那列、
    有时漏掉。结果同一个表里，`应收票据 6,493,643.05` 会落到「期初」栏，
    而别的行落到「期末」栏 —— **金额放错栏，而且不报错。**

    财务数据上这类静默错误比明显报错危险得多，所以宁可多算一步。
    """
    xs = sorted(x for row in rows for x, _ in row)
    if not xs:
        return []
    centers: list[float] = [xs[0]]
    for x in xs[1:]:
        if x - centers[-1] > _COL_TOL:
            centers.append(x)
    return centers


def _column_of(x: float, centers: list[float]) -> int:
    return min(range(len(centers)), key=lambda i: abs(centers[i] - x))


def count_repairs(rows: list[list[tuple[float, str]]]) -> int:
    """这一页有多少个金额是**修出来的**（不是原样读对的）。

    单独统计是为了让报告能说清「这些数我动过」——
    静默修复和静默凑数一样不可接受。
    """
    n = 0
    for row in rows:
        for c in row:
            if parse_amount(c[-1])[1] == "repaired":
                n += 1
    return n


def _norm_cells(rows):
    """把单元格统一成 `(x, y, 文本)`。y 允许缺失（旧格式/测试用）。"""
    out = []
    for row in rows:
        cells = []
        for c in row:
            if len(c) >= 3:
                cells.append((float(c[0]), c[1], str(c[2])))
            else:
                cells.append((float(c[0]), None, str(c[1])))
        out.append(cells)
    return out


def _label_columns_x(rows, centers) -> list[int]:
    """哪些**列**装的是科目名（而不是数值）。

    判据：这一列里「不像数值」的单元格占比高。
    """
    hits: dict[int, list[int]] = {}
    for row in rows:
        for x, _, t in row:
            if not t.strip():
                continue
            c = _column_of(x, centers)
            h = hits.setdefault(c, [0, 0])
            h[1] += 1
            if not _is_value_like(t):
                h[0] += 1
    return sorted(c for c, (num, tot) in hits.items()
                  if tot >= 3 and num / tot >= 0.5)


def split_two_sided(rows):
    """把「左右两栏」的表按列切开，返回若干块。

    ## 为什么（实测：某上市公司 2022 审计报告）

    这份 A 股审计报告的合并资产负债表是 **T 型布局**：
    左半是资产（科目名 x≈0.10），右半是负债及所有者权益（科目名 x≈0.52），
    **同一行里装着两边的科目和金额**：

        行14: 应收账款 x=0.10 | 23,504,762.10 x=0.31 | 48,724,204.22 x=0.42
             | 应付账款 x=0.52 | 699,789,636.47 x=0.72 | 328,606,257.50 x=0.83

    不切开的话，两边会被混成一行，取值时拿到**另一边的金额** ——
    资产科目的行显示负债科目的数字，**而且不报错**。

    （Excel 那份小企业报表是同样的问题，见 `ingest/excel.py`。）
    """
    rows = _norm_cells(rows)
    centers = cluster_columns([[(x, t) for x, _, t in r] for r in rows])
    if not centers:
        return [rows]

    label_cols = _label_columns_x(rows, centers)
    if len(label_cols) < 2:
        return [rows]

    cut_x = centers[label_cols[1]]
    left, right = [], []
    for row in rows:
        left.append([c for c in row if c[0] < cut_x])
        right.append([c for c in row if c[0] >= cut_x])
    return [left, right]


def _is_numeric_cell(text: str) -> bool:
    """这一格是不是一个**可用**金额（含按明确判据修好的）。"""
    _, status = parse_amount(text)
    return status in ("ok", "dash", "repaired")


def _is_value_like(text: str) -> bool:
    """这一格在版面上是不是占着值列（哪怕写错了）。

    用来分列 —— `334.719.50` 该占值列，但它是**可疑值**，要带 `？` 标记。
    这两个判断必须分开：合并成一个的话，可疑值会被当成正常值，
    `？` 标记丢失，静默错误就漏过去了（实测踩到）。
    """
    if _is_numeric_cell(text) or _is_row_number(text):
        return True
    _, status = parse_amount(text)
    return status == "suspect" and is_amount_ish(text)


def rows_to_table(rows: list[list[tuple[float, str]]]) -> list[list[str]]:
    """把 OCR 的行还原成 `[标签, 附注?, 本期值, 上期值]`。

    和 `ingest/layout.py` 的输出结构对齐，这样上层取值不用区分材料是
    「有文字层的排版表」还是「扫描件 OCR」。

    ## 标签列不是「第 0 列」，是「第一个数字列左边的一切」（实测踩到）

    中文财报里**普通科目名左对齐、合计行右对齐**（贴着行次列）：

        货币资金        x=0.177
        流动资产合计     x=0.271      ← 同一个视觉列，但 x 差得很远
        资产总计        x=0.288

    按 x 聚类会把它们分成两列，只取最左列就取空了 —— 于是所有合计行
    都变成「〔此行没有标签〕」，资产总计、负债合计全丢，**勾稽直接判不了**。

    ## ⚠ 已知未解决：T 型（左右两栏）资产负债表

    有些材料（实测某上市公司 2022 审计报告）的资产负债表是左右两栏，
    一行里同时装着资产和负债。这时本函数会**把两边混进同一行**，
    取值时拿到另一边的金额。

    试过按 x 切开（见 `split_two_sided`），但在已跑通的三份材料上
    造成了回归（某非上市公司的现金勾稽从「平」变成「数据不足」），
    所以**没有启用**。目前靠勾稽校验兜底 —— 混栏时勾稽会报
    「数据不足」或「不平」，不会静默给出一个错数。
    """
    return _one_block(_norm_cells(rows))


def _one_block(rows: list[list[tuple[float, str]]]) -> list[list[str]]:
    """处理**单栏**的一块。"""
    centers = cluster_columns([[(x, t) for x, _, t in r] for r in rows])
    if not centers:
        return []

    grid: list[dict[int, list[str]]] = []
    for row in rows:
        cells: dict[int, list[str]] = {}
        for x, _, text in row:
            t = text.strip()
            if not t:
                continue
            cells.setdefault(_column_of(x, centers), []).append(t)
        grid.append(cells)

    # 每列的数字占比 —— 用来定位第一个「数字列」
    def numeric_ratio(c: int) -> tuple[int, float]:
        hits = total = 0
        for cells in grid:
            for t in cells.get(c, []):
                total += 1
                if _is_value_like(t):
                    hits += 1
        return total, (hits / total if total else 0.0)

    numeric_cols = [c for c in range(len(centers)) if numeric_ratio(c)[1] >= 0.5]
    if not numeric_cols:
        return []
    first_numeric = min(numeric_cols)

    # 「行次 / 附注」列：数字列里那些几乎全是小整数的
    note_col = None
    for c in numeric_cols:
        hits = total = 0
        for cells in grid:
            for t in cells.get(c, []):
                total += 1
                if _is_row_number(t):
                    hits += 1
        if total >= 3 and hits / total >= 0.7:
            note_col = c
            break

    value_cols = [c for c in numeric_cols if c != note_col]

    out: list[list[str]] = []
    for cells in grid:
        if not cells:
            continue
        # 标签 = 第一个数字列左边的所有列，按 x 顺序拼起来
        label = "  ".join(
            t for c in range(first_numeric) for t in cells.get(c, [])
        ).strip()
        note = "  ".join(cells.get(note_col, [])).strip() if note_col is not None else ""

        values: list[str] = []
        for c in value_cols:
            for t in cells.get(c, []):
                if _is_row_number(t) and note_col is None:
                    continue                      # 没识别出行次列时也别把它当值
                v, status = parse_amount(t)
                if status == "suspect" and is_amount_ish(t):
                    values.append(f"？{t}")       # 判据不明确 → 可见地标出
                elif status == "repaired":
                    # **必须以修复后的形式输出。**
                    # 输出原文的话，下游 `_to_number("334.719.50")` 会剥成
                    # 33471950 —— 差 100 倍且不报错。
                    values.append(repair_systematic(t) or t)
                else:
                    values.append(t)

        if not label and not values:
            continue
        if not label:
            label = "〔此行没有标签〕"

        padded = ([""] * 2 + values)[-2:]
        out.append([label, note, *padded])
    return out


def rows_to_text(rows: list[list[tuple[float, str]]]) -> str:
    """把 OCR 的行拼回一行行的正文，供检索和排版表解析用。"""
    lines = []
    for row in rows:
        parts = [c[-1].strip() for c in sorted(row, key=lambda p: p[0])
                 if c[-1] and c[-1].strip()]
        if parts:
            lines.append("  ".join(parts))
    return "\n".join(lines)
