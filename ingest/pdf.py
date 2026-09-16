"""PDF 抽取 —— 文字 + 表格，**可选依赖 pdfplumber**。

## 为什么这里破例加了依赖

项目其余部分是零依赖的（`guard.py`、`retrieval.py`、`valuation/` 全是纯标准库）。
PDF 是个例外：它是二进制格式，带压缩流和字体子集，
**零依赖解析不现实**。

所以做法是：**依赖做成可选的**。没装 pdfplumber 时，
这个模块给出明确的安装指引，而项目的其余部分照常工作。

## 为什么用 pdfplumber 而不是 pdftotext

对财务三张表，**表格结构比文字重要得多**。`pdftotext` 会把资产负债表
抽成流式文本，行列对应关系就丢了 —— "3,180" 到底是货币资金还是应收账款，
从文本里看不出来。

`pdfplumber.extract_tables()` 保留行列网格，这是它的核心价值。

## 必须做的一件事：归一化

PDF 抽出来的中文，码位经常不是汉字本字。实测一份 1079 字的财报里
有 16 个字符是康熙部首（`⾦` U+2FA6 而不是 `金` U+91D1）。

**看着一样，`==` 比较是 False。** 会静默毁掉检索和科目匹配。
所有文本出这个模块前一律过 `normalize_text()`。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import compat_issues, normalize_text, unfixable_issues

# pdfminer 会对字体子集刷大量 "Could not get FontBBox..." 警告，
# 那是噪音，不影响抽取结果。**但只在导入 pdfplumber 之前压掉**，
# 不去动全局日志配置 —— 别人的日志设置不该被我改。
_PDFMINER_LOGGERS = ("pdfminer", "pdfplumber")


class PdfDependencyMissing(RuntimeError):
    """没装 pdfplumber。给出可操作的安装指引，不是干巴巴的 ImportError。"""


def _import_pdfplumber():
    for name in _PDFMINER_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - 环境相关
        raise PdfDependencyMissing(
            "读 PDF 需要 pdfplumber（项目里唯一一个可选依赖）。\n"
            "装它：\n"
            "    python3 -m pip install pdfplumber\n\n"
            "**不装也不影响其他功能** —— .txt / .md 材料照常处理，\n"
            "估值引擎、可比公司、假设参谋都走标准库。"
        ) from e
    return pdfplumber


@dataclass
class PdfPage:
    number: int                 # 从 1 开始
    text: str                   # 已归一化，**换行已折叠成空格**（给检索用）
    tables: list[list[list[str | None]]] = field(default_factory=list)
    #: 保留**原始换行**的正文（只过归一化，不折叠换行）。
    #:
    #: ## 为什么两份都要留（实测踩到）
    #:
    #: `text` 把 PDF 的换行折叠成空格 —— 对正文检索是对的
    #: （PDF 的换行多半是排版换行，不是语义换行）。
    #:
    #: 但这**正好摧毁了无框线排版表的行结构**：某 H 股公司年报里
    #: `Property, plant and equipment 物業、廠房及設備 11 26,189 60,057`
    #: 被折成一整页一行，按行解析无从下手。
    #:
    #: 所以两个都留：检索用 `text`，排版表解析用 `raw_text`。
    raw_text: str = ""
    #: 这一页的文字是 **OCR 出来的**，不是原生文字层。
    #:
    #: 下游要区别对待 —— OCR 出来的数字有认错的可能（实测抓到过
    #: `334,719.50` 被认成 `334.719.50`），该人工复核。
    #: 和原生文字一视同仁是不诚实的。
    ocr: bool = False

    def usable_tables(self) -> list[list[list[str]]]:
        """能用的表格。**表格太窄时退回按行解析。**

        ## 什么时候会退回（实测踩到）

        `pdfplumber` 默认按**线条**找表格。某 H 股公司 2020 年报（H 股）的
        财务报表没有框线，于是它只抽出数字列：

            行2: ['26,189']        ← 标签完全丢了

        而同一页正文里信息齐全。H 股 / IFRS 年报普遍用无框线排版表，
        所以这个兜底不是特例处理。

        判据：**最宽的一行少于 2 列** —— 一个连标签都没抓到的"表格"
        不可能是有用的表格。
        """
        from .layout import parse_layout_lines

        if self.tables:
            widest = max((len(t[0]) for t in self.tables if t and t[0]), default=0)
            if widest >= 2:
                return self.tables
        layout = parse_layout_lines(self.raw_text or self.text)
        if layout:
            return [layout]
        return self.tables

    def tables_markdown(self) -> str:
        """把这一页的表格渲染成 markdown。

        给 LLM 看的时候，markdown 表格比原始网格更能保持行列语义。

        **标题里带页码**：分块之后，一个片段可能落在表格中间，
        拿不到正文开头那个 `【第 N 页】`，引用就只能退化成「第 N 段」。
        每个表头自带页码，任何位置的片段都还知道自己在哪一页。
        """
        out: list[str] = []
        for i, table in enumerate(self.tables, 1):
            out.append(f"【第 {self.number} 页 · 表 {i}】")
            for row in table:
                cells = [(c or "").replace("\n", " ").strip() for c in row]
                out.append("| " + " | ".join(cells) + " |")
            out.append("")
        return "\n".join(out)


@dataclass
class PdfDocument:
    path: Path
    pages: list[PdfPage]
    compat_chars: list[str] = field(default_factory=list)
    unmapped_chars: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 走了 OCR 的页码。**报告里要说出来** —— OCR 出来的数字该人工复核。
    ocr_pages: list[int] = field(default_factory=list)
    #: 其中有多少个金额是**按判据修出来的**（不是原样读对的）。
    #: 静默修复和静默凑数一样不可接受，所以要单独报出来。
    ocr_repaired: int = 0

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_text(self, include_tables: bool = True) -> str:
        """整份文档 → 带页码标记的文本。

        **页码标记是有意的**：尽调结论必须能指到「哪一页」，
        而现有的分块和检索是按文本走的。把页码留在正文里，
        分块之后它自然跟着走，引用的粒度就止于段落而不是整份文件。
        """
        parts: list[str] = []
        for p in self.pages:
            parts.append(f"\n【第 {p.number} 页】\n")
            if p.text.strip():
                parts.append(p.text.strip())
            if include_tables and p.tables:
                parts.append("")
                parts.append(p.tables_markdown().strip())
        return "\n".join(parts).strip()

    def summary(self) -> str:
        n_tables = sum(len(p.tables) for p in self.pages)
        lines = [
            f"{self.path.name}：{self.page_count} 页，"
            f"{sum(len(p.text) for p in self.pages):,} 字，{n_tables} 个表格"
        ]
        if self.compat_chars:
            lines.append(
                f"⚠ 归一化修正了 {len(self.compat_chars)} 个兼容性字符"
                f"（康熙部首/全角变体）—— 不修正的话检索会静默失配"
            )
        if self.unmapped_chars:
            shown = "  ".join(f"{c}(U+{ord(c):04X})" for c in self.unmapped_chars[:8])
            lines.append(
                f"⚠ {len(self.unmapped_chars)} 个字符在部首区但**没有映射**：{shown}\n"
                f"    这些字我没把握对应到哪个正字，**拒绝猜**。请人工确认。"
            )
        for w in self.warnings:
            lines.append(f"⚠ {w}")
        return "\n".join(lines)


# 空白规范化：PDF 里的换行多半是排版换行，不是语义换行。
# 单换行折叠成空格（并保留段落边界），中文字之间的空格去掉。
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_SINGLE_NEWLINE = re.compile(r"(?<![。；：！？])[\r\n]+(?![【第])")
_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])")


def _clean(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _MULTI_NEWLINE.sub("\n\n", t)
    t = _SINGLE_NEWLINE.sub(" ", t)
    # PDF 常在汉字之间插入多余空格（字距导致），去掉
    t = _CJK_SPACE.sub("", t)
    return t.strip()


def _clean_lines(text: str) -> str:
    """只做最小清理，**保留换行结构**。

    给无框线排版表的按行解析用。`_clean` 会把单换行折成空格
    （对正文检索是对的），但那样排版表就没有行了。
    """
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _CJK_SPACE.sub("", t)
    return t.strip()


def _contiguous_runs(nums: list[int]) -> list[tuple[int, int]]:
    """把页码列表并成连续区间。

    OCR 一次进程跑一段最划算（每页约 1 秒，但每次启动 swift 也要约 1 秒）。
    一份混合型 PDF 里扫描页往往连着，并成区间能少启动几次。
    """
    if not nums:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append((start, prev))
        start = prev = n
    runs.append((start, prev))
    return runs


def extract_pdf(path: str | Path, extract_tables: bool = True,
                ocr_fallback: bool = True) -> PdfDocument:
    """抽一份 PDF 的文字和表格。

    ## 文字层为空时回落到 OCR

    扫描件（图片型 PDF）的文字层是空的。这时：

    - 这台机器能跑 OCR（macOS + swift）→ **自动用 Vision 框架识别**，
      并把页码记进 `ocr_pages`，让下游知道这些数字该人工复核
    - 跑不了 → **明确报出来**，而不是给你一份空文本让你以为材料里没内容

    OCR 结果会缓存（见 `ingest/ocr.py`），同一份材料第二次读不重跑。
    """
    pdfplumber = _import_pdfplumber()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")

    pages: list[PdfPage] = []
    warnings: list[str] = []
    all_raw = ""

    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            raw = page.extract_text() or ""
            all_raw += raw
            tables: list[list[list[str | None]]] = []
            if extract_tables:
                try:
                    for t in page.extract_tables() or []:
                        tables.append([
                            [normalize_text(_clean(c)) if c else c for c in row]
                            for row in t
                        ])
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"第 {i} 页表格抽取失败（{type(e).__name__}）：{e}")
            pages.append(PdfPage(number=i, text=normalize_text(_clean(raw)),
                                 raw_text=_clean_lines(raw),
                                 tables=tables))

    ocr_used, ocr_fixed = _fill_blank_pages_with_ocr(path, pages, warnings) if ocr_fallback else ([], 0)

    total_chars = sum(len(p.text) for p in pages)
    if total_chars == 0:
        detail = "" if ocr_fallback else "（本次调用关掉了 OCR 回落）"
        warnings.append(
            "**整份文件没有抽到任何文字。** 大概率是扫描件（图片型 PDF）。"
            f"{detail}\n"
            "  别把这份空文本当成「材料里没有内容」。"
        )
    elif len(ocr_used) == len(pages):
        warnings.append(
            f"**全文 {len(pages)} 页都走了 OCR**（这原本是扫描件）。\n"
            "  OCR 会认错字和标点，**数字请人工复核**：报告里可疑的金额会带 `？` 前缀。"
        )
    elif ocr_used:
        warnings.append(
            f"其中 {len(ocr_used)} 页（{ocr_used[:8]}"
            f"{'…' if len(ocr_used) > 8 else ''}）是扫描页，走了 OCR —— "
            "这些页的数字请人工复核。"
        )
    elif total_chars < 40 * len(pages):
        warnings.append(
            f"每页平均只有 {total_chars // max(len(pages), 1)} 字，明显偏少 —— "
            f"可能是扫描件，也可能表格是图片。**先人工翻一眼再下结论。**"
        )

    return PdfDocument(
        path=path, pages=pages,
        compat_chars=compat_issues(all_raw),
        unmapped_chars=unfixable_issues(all_raw),
        warnings=warnings, ocr_pages=ocr_used, ocr_repaired=ocr_fixed,
    )


def _fill_blank_pages_with_ocr(path: Path, pages: list[PdfPage],
                               warnings: list[str]) -> tuple[list[int], int]:
    """把「文字层为空」的页用 OCR 补上。

    返回 `(补过的页码, 其中修出来的金额个数)`。
    """
    blank = [p.number for p in pages if not p.text.strip()]
    if not blank:
        return [], 0

    try:
        from . import ocr
    except ImportError:  # pragma: no cover
        warnings.append("OCR 模块加载失败，跳过。")
        return [], 0

    if not ocr.available():
        warnings.append(
            f"有 {len(blank)} 页文字层为空（扫描页），但**这台机器跑不了 OCR**："
            f"{ocr.why_unavailable()}"
        )
        return [], 0

    filled: list[int] = []
    repaired = 0
    for start, end in _contiguous_runs(blank):
        try:
            got = ocr.ocr_pages(path, start, end)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"OCR 第 {start}–{end} 页失败：{type(e).__name__}：{e}")
            continue
        for n, rows in got.items():
            if not (1 <= n <= len(pages)) or not rows:
                continue
            page = pages[n - 1]
            repaired += ocr.count_repairs(rows)
            page.text = ocr.rows_to_text(rows)
            page.raw_text = page.text
            page.tables = [[list(r) for r in ocr.rows_to_table(rows)]]
            page.ocr = True
            filled.append(n)
    return filled, repaired
