"""跨域防护 —— 机密域里唯一允许发起公开域请求的入口。

## 为什么这个模块比 `net.py` 重要

`net.py` 里的格式检查（长度、字符集、换行）**已被实测证明基本无效**：

```
样本                     长度   格式检查结果
完整段落（45 字）         45     ✗ 通过了
财务句（53 字）           53     ✗ 通过了
短句（24 字）             24     ✗ 通过了
```

原因：语料里句子混着数字和标点，中文串被打断，"连续中文超过 N 个字就拒绝"
这类规则根本触发不了。而放宽长度上限会连正常查询一起拦掉。

**所以真正的防线是这一条：出境的值不能与本地语料有任何实质重合。**

这个检查必须是确定性的、可验证的——不是"我觉得这段像材料"，
而是"这段文字在本地语料里逐字存在"。

## 三层防护（按重要性排序）

1. **代码路径隔离**（根本保证）—— 公开域的函数只接受代码/日期/指标名，
   调用点写在读不到语料的代码里。这一层不靠检查，靠结构。
2. **语料重叠检查**（本模块）—— 出境值与本地语料逐字比对，重合即拒。
3. **格式检查**（`net.py`）—— 防误伤，不是主防线。

## 还有一个容易漏的

**标的公司的名字本身也是机密。** 你不会想让人从查询日志里看出你在看哪家公司。

所以除了"不能抄材料"，还有一条"不能用被禁名字"——
禁名表从 `root/privacy-blocklist.txt` 读取，属于你的私有资产。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import net

ROOT = Path(__file__).resolve().parent
BLOCKLIST_PATH = ROOT / "root" / "privacy-blocklist.txt"

# 归一化时去掉的字符：空白、标点、全角空格
_STRIP = re.compile(r"[\s\u3000，。、；：？！“”‘’（）《》【】,.;:?!\"'()\[\]{}<>\-—_/\\|*#]+")

# 少于这个长度的值不参与语料比对（太短，无法判定）
MIN_OVERLAP = 8


class PrivacyViolation(RuntimeError):
    """出境请求被跨域防护拦下。"""


def normalize(text: str) -> str:
    """归一化：去掉空白和标点，便于逐字比对。

    这样 "2025 年营业收入 18,300 万元" 和 "2025年营业收入18300万元"
    会被认作同一串 —— 改写标点不能绕过检查。
    """
    return _STRIP.sub("", text).lower()


def load_blocklist(path: Path | None = None) -> set[str]:
    """读取禁名表。每行一个词，# 开头是注释。

    `root/privacy-blocklist.txt` 在 .gitignore 里 —— 这是你的私有资产。
    """
    p = path or BLOCKLIST_PATH
    if not p.exists():
        return set()
    terms: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.add(line)
    return terms


def overlaps_corpus(value: str, corpus_normalized: str, min_overlap: int = MIN_OVERLAP) -> str | None:
    """返回值与语料重合的那一段；没有重合返回 None。

    做法朴素但确定：把值归一化后，逐窗口检查是否在归一化语料里出现。
    归一化之后改写标点、加空格都绕不过去。
    """
    v = normalize(value)
    if len(v) < min_overlap:
        return None
    # 逐个窗口从大到小找，找到即返回（先试更长的，命中更有说服力）
    for size in range(len(v), min_overlap - 1, -1):
        for i in range(len(v) - size + 1):
            window = v[i:i + size]
            if window in corpus_normalized:
                return window
    return None


def iter_values(params: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """把参数展平成 (键, 字符串值)。非字符串值跳过。"""
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                if isinstance(v, str):
                    yield f"{key}[{i}]", v
        elif isinstance(value, str):
            yield key, value


def check_public_params(
    params: dict[str, Any],
    corpus_text: str = "",
    blocklist: set[str] | None = None,
) -> None:
    """出境前的全部检查。任何一条不过就抛 PrivacyViolation。"""
    blocked = blocklist if blocklist is not None else load_blocklist()
    corpus_norm = normalize(corpus_text) if corpus_text else ""

    for key, value in iter_values(params):
        # 1) 禁名表
        for term in blocked:
            if term and term in value:
                raise PrivacyViolation(
                    f"参数 {key!r} 含禁名「{term}」—— 标的公司名不得出境。"
                    f"改用行业代码或公开的量化条件查询。"
                )

        # 2) 语料重叠（这条是主防线）
        if corpus_norm:
            hit = overlaps_corpus(value, corpus_norm)
            if hit:
                raise PrivacyViolation(
                    f"参数 {key!r} 与本地语料重合 {len(hit)} 个字：「{hit[:30]}…」"
                    f"—— 材料内容不得出境。"
                )


def safe_public_get(
    url: str,
    params: dict[str, Any],
    corpus_text: str = "",
    purpose: str = "",
    timeout: int = 30,
    extra_hosts: frozenset[str] = frozenset(),
    headers: dict[str, str] | None = None,
) -> bytes:
    """机密域发起公开域请求的**唯一入口**。

    顺序：跨域检查 → 公开域闸门 → 出网。

    注意参数名：这里要显式传 `corpus_text`，也就是调用方必须手里有语料才能检查。
    反过来，`net.guarded_get` 不需要语料 —— 那一侧的代码根本拿不到语料。
    """
    check_public_params(params, corpus_text)

    query = net.PublicQuery(params=params, purpose=purpose, extra_hosts=extra_hosts)
    return net.guarded_get(url, query, timeout=timeout, headers=headers)
