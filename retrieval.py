"""本地检索 —— 纯标准库，零依赖。

用 BM25 + 中文字符二元组。理由：
- 中文没有空格分词，字符 bigram 对这种"找具体数字和条款"的任务效果好，
  而且不需要下载词典（保持了零依赖、可审计的特性）。
- 财务材料里的关键信息是精确串（"EBITDA 3,150 万元"、"71%"），
  词频检索比向量检索更靠得住。

后续可以加向量召回做混合检索——接口已经留好（见 retrieve() 的 scores 参数）。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

CJK = r"\u4e00-\u9fff\u3400-\u4dbf"

#: 页码标记。两种形式都要认：
#:   【第 3 页】            正文里的页分隔（ingest/pdf.py 写入）
#:   【第 3 页 · 表 2】     表格标题（分块后落点可能在表格中间，表头也得带页码）
PAGE_MARK = re.compile(r"【第 (\d+)(?:[–\-](\d+))? 页")


def tokenize(text: str) -> list[str]:
    """中英混排分词：ASCII 词 + 中文字 + 中文二元组。"""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9.%]*", text)
    chars = re.findall(f"[{CJK}]", text)
    tokens.extend(chars)
    tokens.extend(a + b for a, b in zip(chars, chars[1:]))
    return tokens


@dataclass
class Chunk:
    """一个可检索片段，保留完整来源信息以便追溯。"""

    chunk_id: str
    source: str
    para_start: int
    para_end: int
    text: str
    tokens: list[str] = field(default_factory=list, repr=False)

    def page_range(self) -> tuple[int, int] | None:
        """片段覆盖的页码（如果文本里有页码标记）。

        页码标记有两种形式，都要认：
          `【第 3 页】`          正文页分隔
          `【第 3 页 · 表 2】`   表格标题

        实测踩过：正则原来要求 `】` 紧跟在 `页` 后面，
        于是表格标题里的页码全认不出来，7 个片段只有 2 个能报到页码。
        """
        pages: list[int] = []
        for m in PAGE_MARK.finditer(self.text):
            pages.append(int(m.group(1)))
            if m.group(2):
                pages.append(int(m.group(2)))
        return (min(pages), max(pages)) if pages else None

    def cite_label(self) -> str:
        """引用的定位标签。

        ## 这里改过一次，原因是实测模型把段落号当成了页码

        原来返回 `{source}#p{段号}`。模型看到 `sample-cim.txt#p5`，
        在回答里写成了「页码 p5」—— 把**段落号误读成了页码**。

        现在分两种情况：
          - 文本里有 `【第 N 页】`（PDF 抽取时写入）→ 直接报页码
          - 没有 → 报「第 N 段」，**不用容易误读的 `#pN` 形式**

        实测改动后模型引用的是「test-financials.pdf 第 1 页」，准确。
        """
        pages = self.page_range()
        if pages is None:
            loc = f"第 {self.para_start} 段"
        elif pages[0] == pages[1]:
            loc = f"第 {pages[0]} 页"
        else:
            loc = f"第 {pages[0]}–{pages[1]} 页"
        return f"{self.source} {loc}"


def load_text(path: Path) -> str:
    """读取纯文本类材料（.txt / .md）。

    PDF 走 `ingest/pdf.py`，Word/Excel 还没做。分派逻辑在
    `freeanalyst.py::_load_material`。
    """
    return path.read_text(encoding="utf-8", errors="replace")


def chunk_document(
    source: str,
    text: str,
    target_chars: int = 420,
    overlap_paras: int = 1,
) -> list[Chunk]:
    """按段落聚合成片段，保留 1 段重叠，避免答案被切断在边界上。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return []

    chunks: list[Chunk] = []
    i = 0
    n = 0
    while i < len(paras):
        buf: list[str] = []
        size = 0
        j = i
        while j < len(paras) and (size < target_chars or j == i):
            buf.append(paras[j])
            size += len(paras[j])
            j += 1
        n += 1
        chunks.append(
            Chunk(
                chunk_id=f"S{n}",
                source=source,
                para_start=i + 1,
                para_end=j,
                text="\n".join(buf),
            )
        )
        i = j - overlap_paras if j - overlap_paras > i else j
        if i >= len(paras):
            break

    for c in chunks:
        c.tokens = tokenize(c.text)
    return chunks


class BM25:
    """标准 BM25（k1=1.5, b=0.75）。"""

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.doc_len = [len(c.tokens) for c in chunks]
        self.avg_len = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.tf: list[Counter] = [Counter(c.tokens) for c in chunks]
        df: Counter = Counter()
        for counter in self.tf:
            df.update(counter.keys())
        n_docs = len(chunks) or 1
        self.idf = {
            term: math.log(1 + (n_docs - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def search(self, query: str, top_k: int = 6, expand: bool = True) -> list[tuple[Chunk, float]]:
        """检索。

        `expand=True` 时会用 `glossary` 做**跨语言查询扩展**：
        中文问题附上英文术语（命中英文材料），英文问题附上中文术语（命中中文材料）。

        为什么放在检索层而不是调用方：BM25 是**词面匹配**，
        中文问「营业收入」在英文 10-K 里一个词都命中不到，
        **检索会返回一堆无关片段，而模型只会诚实地说"材料未提供"** ——
        看起来像材料里真的没有，实际是没检索到。
        这件事必须自动做，不能靠调用方记得。
        """
        if expand:
            from glossary import expand_query
            query = expand_query(query)
        q_tokens = tokenize(query)
        scored: list[tuple[Chunk, float]] = []
        for idx, chunk in enumerate(self.chunks):
            score = 0.0
            length = self.doc_len[idx] or 1
            for term in q_tokens:
                freq = self.tf[idx].get(term)
                if not freq:
                    continue
                denom = freq + self.k1 * (1 - self.b + self.b * length / self.avg_len)
                score += self.idf.get(term, 0.0) * freq * (self.k1 + 1) / denom
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]
