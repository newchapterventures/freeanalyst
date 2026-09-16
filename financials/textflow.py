"""从「连续文字流」里把表格切回来。

## 问题（实测：蓝色光标、安集科技等现代 A 股年报）

这些年报的三张表**没有表格结构** —— 科目名和金额排成一条连续的文字流：

    流动资产:
    货币资金 2,826,966,781.73 2,779,185,080.64 结算备付金拆出资金交易性金融资产
    1,189,982,759.28 1,290,138,477.89 衍生金融资产应收票据 4,000,000.00 应收账款 7,875,546,85...

`pdfplumber.extract_tables()` 在这种页上会返回行，但**值列是空的**
（实测蓝色光标 2025 年报第 70 页：35 行，0 行带值）。

## 做法

规律其实很整齐：**中文科目名紧跟 1–2 个金额**。所以按
「中文串 + 金额」扫描。难点是**没有金额的科目会连成一片**：

    结算备付金拆出资金交易性金融资产 1,189,982,759.28

这里其实是三个科目，只有最后一个有金额。

**解法**：拿已知科目名表，从右往左剥 —— 每次都取「最长的、是当前串后缀的
已知科目名」。剥出来的是 `交易性金融资产` / `拆出资金` / `结算备付金` ✓

金额归给**紧挨着它的那个科目**（也就是最先剥出来的那个）。
"""

from __future__ import annotations

import re

_AMOUNT = r"(?:\(?-?[\d,]+(?:\.\d+)?\)?|—|–|-)"

#: 「中文串 + 1~3 个数字」的片段。
#:
#: **要允许三个数字** —— 现代 A 股年报的表是
#: `科目名 行次 期末 期初`，三个数。只吃两个的话行次会被当成期末值，
#: 而真正的期末值丢掉（实测安集科技 2022 年报就是这样）。
_PIECE = re.compile(
    r"([\u4e00-\u9fff][\u4e00-\u9fff（）()、·]*)"
    r"[\s\u3000]*"
    r"(" + _AMOUNT + r"(?:[\s\u3000]+" + _AMOUNT + r"){0,2})"
)

#: 行次不会超过这个数
_MAX_ROW_NUMBER = 400

#: 只要不是这些，就不当科目名后缀去剥
_MIN_NAME = 2


def _build_index(known_names) -> dict[int, set[str]]:
    """按长度倒排已知科目名，便于「最长后缀」匹配。"""
    idx: dict[int, set[str]] = {}
    for n in known_names:
        n = (n or "").strip()
        if len(n) >= _MIN_NAME:
            idx.setdefault(len(n), set()).add(n)
    return idx


def strip_names(run: str, index: dict[int, set[str]]) -> list[str]:
    """把一个中文串从右往左剥成若干已知科目名。

    剥不动的残渣**直接丢掉** —— 那是没金额的段标题或我们不认识的科目，
    留着会以「乱码科目名」的形式污染下游。
    """
    out: list[str] = []
    s = run
    while s:
        hit = None
        for size in sorted(index, reverse=True):
            if size > len(s):
                continue
            if s[-size:] in index[size]:
                hit = s[-size:]
                break
        if hit is None:
            # 抬不动就砍掉最右一个字再试（残渣处理）
            s = s[:-1]
            continue
        out.append(hit)
        s = s[:-len(hit)]
    return out


def known_names() -> list[str]:
    """所有已知科目名 —— 文字流解析靠它把粘在一起的科目名剥开。"""
    from . import canonical as cn
    return [n for m in cn.MAPPINGS for n in m.names]


def parse_textflow(text: str, names: list[str]) -> list[list[str]]:
    """把连续文字流切成 `[标签, 注, 本期值, 上期值]`。

    和 `ingest/layout.py` / `ingest/ocr.py` 的输出结构对齐，
    这样上层取值逻辑不用区分材料形态。
    """
    index = _build_index(names)
    out: list[list[str]] = []

    for m in _PIECE.finditer(text or ""):
        run, vals = m.group(1), m.group(2).strip()
        if not vals:
            continue
        numbers = [v for v in re.split(r"[\s\u3000]+", vals) if v]
        names = strip_names(run, index)
        if not names:
            # 科目名认不出来 —— 宁可丢掉，也不要造一个假科目
            continue

        # **三个数时，第一个是「行次」不是金额。**
        # 判据：纯数字、无千分位、无小数点、值不大。
        note = ""
        if len(numbers) >= 3:
            head = numbers[0]
            if (not note and head.isdigit() and len(head) <= 3
                    and int(head) <= _MAX_ROW_NUMBER):
                note, numbers = head, numbers[1:]
            else:
                numbers = numbers[-2:]

        label = names[0]
        padded = ([""] * 2 + numbers)[-2:]
        out.append([label, note, *padded])

        # 同串里剥出来的其余科目没有金额，单独成行（下游会跳过）
        for extra in names[1:]:
            out.append([extra, "", "", ""])

    return out


def looks_like_textflow(text: str, known_names) -> bool:
    """这一段文字像不像「连续文字流」的报表。

    判据：能匹配到足够多的「科目名 + 金额」片段。
    """
    index = _build_index(known_names)
    hits = 0
    for m in _PIECE.finditer(text or ""):
        if m.group(2).strip() and strip_names(m.group(1), index):
            hits += 1
            if hits >= 8:
                return True
    return False
