"""无框线排版表的按行解析。

## 为什么需要这个（实测踩到）

`pdfplumber.extract_tables()` 默认按**线条**找表格。
某 H 股公司 2020 年报（H 股）的财务报表**没有框线**，于是它只抽出数字列：

    行2: ['26,189']        ← 标签完全丢了

而同一页的正文里信息是齐全的：

    Property, plant and equipment 物業、廠房及設備 11 26,189 60,057

换 `vertical_strategy="text"` 更糟 —— 它把单词拆成字符
（`Consolidated` → `C` / `onsolid` / `ated`）。

**H 股 / IFRS 年报普遍用无框线排版表**，所以这个兜底必须要有。

## 做法

按行解析：从行尾往前收数字，最后两个是「本期 / 上期」，
再往前的数字属于标签（附注编号），剩下的就是标签。
"""

from __future__ import annotations

import re

#: 像数字的词元（含千分位、括号负数、破折号占位）
_NUM_TOKEN = re.compile(r"^[（(]?[-–—]?\d[\d,]*(?:\.\d+)?[)）]?$|^[-–—]$")


def _is_number(tok: str) -> bool:
    return bool(_NUM_TOKEN.match(tok))


#: 被空格拆开的数字：`1,079,71` + `6` → `1,079,716`
#:
#: 右对齐的大数字在 PDF 里会被从中间断开（实测：某 H 股公司年报里
#: `1,079,716` 抽出来是 `1,079,71 6`）。判据是**千分位分组不足 3 位** ——
#: 正常写法 `1,079,716` 的最后一组一定是 3 位，只有 2 位说明被截断了。
#: 这条判据不会误伤 `20,282 13,660`（最后一组都是 3 位）。
_SPLIT_NUMBER = re.compile(r"(\d[\d,]*,\d{1,2})\s+(\d{1,3})(?![\d,])")


#: 括号负数里的空格：`(601,579 )` → `(601,579)`
#:
#: 某 H 股公司年报里负数写成 `(601,579 )` —— **右括号前有空格**。
#: 按空格切词后 `)` 单独成一个词元，它不是数字，于是「从行尾收数字」
#: 第一步就中断，整行被当成没有值的标签行，金额全丢。
_PAREN_SPACE = re.compile(r"([（(])\s*([\d,]+(?:\.\d+)?)\s*([)）])")

#: 括号内数字被空格断开：`(55 5)` → `(555)`
#:
#: 和 `_SPLIT_NUMBER` 是一回事，但这个**没有千分位逗号**，
#: 所以那条正则抓不到（实测：某 H 股公司年报 `非控制性權益 (55 5)`）。
_PAREN_SPLIT = re.compile(r"([（(]\d[\d,.]*)\s+(\d+[)）])")


#: 只有数字、没有标签的行。**不是正常情况，要让它可见。**
_UNLABELED = "〔此行没有标签〕"


def _starts_lower(s: str) -> bool:
    """这一行是不是以小写 ASCII 字母开头。

    ## 这是判断「标签还没写完」的可用信号（实测踩到）

    某 H 股公司年报里长标签会折行，值留在**最后一行**：

        Exchange differences on translation of 換算本公司及海外附屬公司
        financial statements of the Company 財務報表的匯兌差額
        and overseas subsidiaries (130,527) 40,455        ← 值在这行

    英文标签折行的下一行**一定小写开头**。所以「累积出的标签以小写字母
    开头」就等于「这句话还没说完，继续往上接」。

    中文没有大小写，所以纯中文材料上这条规则不生效 ——
    那时标签本来就短，很少折行。
    """
    return bool(s) and s[0].isascii() and s[0].islower()


def _group_pending(pending: list[str]) -> list[str]:
    """把攒下来的「不带值的行」分组 —— 折行的归一行，段标题各自独立。

    用的是和标签拼接同一条判据（`_starts_lower`）：小写开头说明是上一句的续行。
    """
    groups: list[str] = []
    for line in pending:
        if groups and _starts_lower(line):
            groups[-1] = f"{groups[-1]} {line}"
        else:
            groups.append(line)
    return groups


def parse_layout_lines(text: str, max_values: int = 2) -> list[list[str]]:
    """把「标签 … 数字 数字」的行解析成表格行。

    返回的行的结构是 `[标签, 附注?, 本期值, 上期值]` —— 与真表格对齐，
    这样上层的取值逻辑不用为它单独写一套。

    **折行标签会被拼回完整。** 不带值的行先攒着；遇到带值的行时，
    从后往前把「仍属同一句」的续行接上（见 `_starts_lower`）。
    攒着但没被接走的（段标题之类）单独成行。
    """
    rows: list[list[str]] = []
    pending: list[str] = []

    for raw in text.splitlines():
        line = _SPLIT_NUMBER.sub(r"\1\2", raw.strip())
        line = _PAREN_SPLIT.sub(r"\1\2", line)
        line = _PAREN_SPACE.sub(r"\1\2\3", line)
        if len(line) < 2:
            continue

        # 半角空格和各宽度空格都当分隔符
        toks = re.split(r"[\s\u3000]+", line)
        toks = [t for t in toks if t]
        if len(toks) < 2:
            continue

        # 从行尾往前收数字
        tail: list[str] = []
        i = len(toks)
        while i > 0 and _is_number(toks[i - 1]):
            tail.insert(0, toks[i - 1])
            i -= 1

        values = tail[-max_values:] if tail else []
        rest_nums = tail[:-max_values] if tail else []
        note = rest_nums[-1] if rest_nums else ""
        own = " ".join(toks[:i] + rest_nums[:-1]).strip()

        if not values:
            # 不带值的行：可能是段标题，也可能是折行标签的前半
            if own:
                pending.append(own)
            continue

        # 带值的行：往上接续行
        label = own
        while pending and _starts_lower(label):
            label = f"{pending.pop()} {label}".strip()

        # 整行只有数字、没有标签（总计行常这样）：用紧邻的上一行当标签，
        # 否则这一行的金额会被整条丢掉。
        if not label and pending:
            label = pending.pop()
        if not label:
            # **标签根本不存在**（实测：某 H 股公司年报里「Total current assets」
            # 那行只有数字，标签被画成了图形，不在文字层里）。
            # 用占位标签让它**可见地**留下来 —— 静默丢掉的话，
            # 用户会以为这张表本来就没有这一行。
            label = _UNLABELED

        # 没被接走的攒行是段标题 —— **合成一行**，不要拆成多行。
        # 「Items that may be reclassified 其後可重新分類至損益的 /
        #  subsequently to profit or loss: 項目：」本来就是一句。
        if pending:
            rows.extend([g, "", "", ""] for g in _group_pending(pending))
        pending.clear()

        if not label:
            continue
        padded = ([""] * max_values + values)[-max_values:]
        rows.append([label, note, *padded])

    # 页尾剩下的攒行
    if pending:
        rows.extend([g, "", "", ""] for g in _group_pending(pending))

    return rows
