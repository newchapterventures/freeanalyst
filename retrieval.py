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

    def cite_label(self) -> str:
        return f"{self.source}#p{self.para_start}"


def load_text(path: Path) -> str:
    """读取纯文本类材料。PDF/Word 支持见 README 的 roadmap。"""
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

    def search(self, query: str, top_k: int = 6) -> list[tuple[Chunk, float]]:
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
