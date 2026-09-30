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


# ─────────────────── 按坐标配对（比按文字流可靠） ───────────────────

#: 附注列与金额列之间的最小横向距离 —— 小于它就不算附注号。
#: 实测（中国人寿第 89 页）：附注列 x≈367，金额列 x≈430 / 530 → 间距 ≈63。
#: 而**同一列的相邻数字**（被空格切开的千分位）间距只有几个点。
_NOTE_GAP = 25.0

#: 行距兜底（拿不到足够样本时用）。实测这类页约 15 点。
_DEFAULT_PITCH = 15.0


def _center_y(w: dict) -> float:
    return (float(w.get("top", 0)) + float(w.get("bottom", 0))) / 2


def _cluster_lines(words: list[dict], tol: float = 3.0) -> list[list[dict]]:
    """把词按**中心 y** 归成视觉行（同一行的中心 y 相差不超过 tol）。"""
    out: list[list[dict]] = []
    for w in sorted(words, key=_center_y):
        if out and abs(_center_y(w) - _center_y(out[-1][-1])) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def looks_like_split_rows(words: list[dict], *, min_rows: int = 6,
                          threshold: float = 0.25) -> bool:
    """这一页是不是「科目名与金额**分成两行**」的版式 —— **只有这种才该按坐标配对**。

    ## 判据是量出来的（2026-09-30，两份公开来源材料）

        错位版式：纯金额行占 **43–44%**，标签与金额同行的只有 1 行
        正常版式：纯金额行占 **2%**，标签与金额同行 18–20 行

    差 20 倍 —— 所以 0.25 这个阈值不是拍脑袋定的。

    ## 为什么必须先判版式，而不是"看结果好不好"
    我第一版的做法是：坐标配对的结果**映射行更多就采用** ✗。错在哪：
    **错配同样会映射出更多行**（甚至把同一个数塞给两个不同科目）。
    实测代价：某 A 股年报的资产总计与所有者权益拿到了同一个数，
    勾稽从"差 184 万"变成"差 172 亿" ✗✗ —— 而这份材料的标签和金额本来就在同一行，
    坐标配对根本没有用武之地。

    **量版式是量事实；量"行数多少"是量我自己的猜。**
    """
    lines = _cluster_lines(words)
    if len(lines) < min_rows:
        return False
    num_only = 0
    for ln in lines:
        has_lab = any(not _is_number(str(w.get("text", ""))) for w in ln)
        has_num = any(_is_number(str(w.get("text", ""))) for w in ln)
        if has_num and not has_lab:
            num_only += 1
    return (num_only / len(lines)) >= threshold


def parse_words_rows(words: list[dict], *, max_values: int = 2) -> list[list[str]]:
    """按**坐标**配对：左边是科目名，右边是金额 —— 金额归给**垂直中心最近**的科目。

    ## 为什么必须有这条（实测：中国人寿 2025 年报第 89/90 页，公开来源）

    那几页的金额格是**垂直居中**的，所以金额行的 `top` 比科目名行**小约 7 点**：

        1   143,319   86,519    中心 y≈138      ← 附注号 + 本期/上期（在【上】）
        货币资金                 中心 y≈145      ← 科目名（在【下】）
        2    50,879   30,560    中心 y≈153
        买入返售金融资产           中心 y≈160

    按 y 从上到下读出来的**文字流**因此是"金额在前、科目名在后" ✗ ——
    而纯文本规则（"整行只有数字时用紧邻的**上一行**当标签"，见上面第 147 行那段）
    会把整张表**错配一位**：实测 `货币资金` 拿到了下一行的 50,879，
    真正的 143,319 判给了上一行；`资产总计` 因为没人接就整个丢了。

    **坐标不会骗人**：行距约 15 点，而偏移只有 7 点 ——
    按中心 y 就近配对，配对距离 7 < 15，不会串到隔壁行。

    ## 输出形状
    与 `parse_layout_lines` 一致：`[科目名, 附注, 本期, 上期, …]`（右侧对齐到 `max_values`）。
    """
    if not words:
        return []

    lines = _cluster_lines(words)

    #: 像**科目名**才算标签行。判据与 `textflow._LABEL_CHARS` 同源：
    #: 科目名是汉字 + 少量标点，**不含数字**（含数字的是表头/日期/页眉），
    #: **不以冒号结尾**（那是「资产：」「负债：」这种段标题）。
    #: ★ 实测为什么不能只靠距离：段标题离第一个金额行 8 点，而真正的科目离它的
    #: 金额 7 点 —— 两者**太接近**，距离分不开；"像不像科目名"能分开。
    def _plausible_label(t: str) -> bool:
        if not t or t.endswith(("：", ":")):
            return False
        return not re.search(r"[0-9A-Za-z]", t)

    label_lines: list[tuple[float, str, bool]] = []
    amount_lines: list[tuple[float, list[tuple[str, float]]]] = []

    for ln in lines:
        left = [w for w in ln if not _is_number(str(w.get("text", "")))]
        nums = [w for w in ln if _is_number(str(w.get("text", "")))]
        if left:
            label = "".join(str(w.get("text", "")) for w in sorted(
                left, key=lambda x: float(x.get("x0", 0)))).strip()
            # 不像科目名的（表头/日期/段标题）：原地留一行，但**不参与配对**
            if not _plausible_label(label):
                rows_txt = "".join(str(w.get("text", "")) for w in sorted(
                    ln, key=lambda x: float(x.get("x0", 0)))).strip()
                label_lines.append((_center_y(ln[-1]), rows_txt, False))
                continue
            if nums:
                label_lines.append((_center_y(ln[-1]), label, True))
                amount_lines.append((_center_y(ln[-1]),
                                     [(str(w.get("text", "")), float(w.get("x0", 0)))
                                      for w in sorted(
                                          nums, key=lambda x: float(x.get("x0", 0)))]))
            else:
                label_lines.append((_center_y(ln[-1]), label, True))
        elif nums:
            amount_lines.append((_center_y(ln[-1]),
                                 [(str(w.get("text", "")), float(w.get("x0", 0)))
                                  for w in sorted(
                                      nums, key=lambda x: float(x.get("x0", 0)))]))

    # 行距（相邻金额行中心距的中位数）—— 用来定"多近才算同一行"
    centers = sorted(ay for ay, _ in amount_lines)
    gaps = [b - a for a, b in zip(centers, centers[1:]) if b - a > 0.5]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else _DEFAULT_PITCH
    #: 配对距离上限 = 行距的一半多一点。
    #: ★ 实测（中国人寿第 89 页）：表头行离第一个金额行 **32 点**，
    #: 而真正的科目离它的金额只有 **7 点**，行距 15 点 ——
    #: 不设上限时表头会把第一个金额行**吃掉**，整表错开两位（真踩过）。
    limit = pitch * 0.55

    # 就近配对：每个金额行只被用一次；科目按 y 顺序处理
    rows: list[list[str]] = []
    used: set[int] = set()
    for ly, label, pairable in sorted(label_lines, key=lambda t: t[0]):
        if not label:
            continue
        if not pairable:
            # 表头 / 日期 / 段标题：原样留一行，但不占金额行
            rows.append([label, "", *([""] * max_values)])
            continue
        best, best_d = None, None
        for i, (ay, nums) in enumerate(amount_lines):
            if i in used or not nums:
                continue
            d = abs(ay - ly)
            if best_d is None or d < best_d:
                best, best_d = i, d
        # 太远就不配 —— 宁可留空让人看见，也不要错配（错配是静默的）
        if best is None or (best_d is not None and best_d > limit):
            rows.append([label, "", *([""] * max_values)])
            continue
        used.add(best)
        nums = amount_lines[best][1]
        # 最左边那个短整数**是不是附注号** —— 不能只看"它是小整数"：
        # ★ 实测：有些页**没有附注列**，那时第一个金额会**被误吃成附注号**，
        # 于是整页金额又错配一位（中国人寿第 90/91 页就是这个）。
        # 判据用**横向距离**：附注列与金额列之间有一大段空白（实测 x≈367 vs 430/530）。
        note = ""
        if len(nums) > 1 and re.fullmatch(r"\d{1,3}", nums[0][0]):
            gap = nums[1][1] - nums[0][1]
            if gap >= _NOTE_GAP:
                note = nums[0][0]
                nums = nums[1:]
        values = [t for t, _x in nums[:max_values]]
        padded = ([""] * max_values + values)[-max_values:]
        rows.append([label, note, *padded])
    return rows
