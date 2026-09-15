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
    text: str                   # 已归一化
    tables: list[list[list[str | None]]] = field(default_factory=list)

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


def extract_pdf(path: str | Path, extract_tables: bool = True) -> PdfDocument:
    """抽一份 PDF 的文字和表格。

    **不做 OCR。** 扫描件抽出来是空的 —— 那种情况会明确报出来，
    而不是给你一份空文本让你以为材料里没内容。
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
                                 tables=tables))

    total_chars = sum(len(p.text) for p in pages)
    if total_chars == 0:
        warnings.append(
            "**整份文件没有抽到任何文字。** 大概率是扫描件（图片型 PDF）。\n"
            "  本工具不做 OCR —— 需要先跑 OCR 再入库。"
            "别把这份空文本当成「材料里没有内容」。"
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
        warnings=warnings,
    )
