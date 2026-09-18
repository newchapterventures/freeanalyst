"""从「连续文字流」里把表格切回来。

## 问题（实测：某 A 股广告公司、某科创板公司等现代 A 股年报）

这些年报的三张表**没有表格结构** —— 科目名和金额排成一条连续的文字流：

    流动资产:
    货币资金 2,826,966,781.73 2,779,185,080.64 结算备付金拆出资金交易性金融资产
    1,189,982,759.28 1,290,138,477.89 衍生金融资产应收票据 4,000,000.00 应收账款 7,875,546,85...

`pdfplumber.extract_tables()` 在这种页上会返回行，但**值列是空的**
（实测某 A 股广告公司 2025 年报第 70 页：35 行，0 行带值）。

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

#: 科目名里允许出现的非汉字字符。
#:
#: **必须包含引号和连字符。** A 股年报的标准科目名长这样：
#:
#:     三、营业利润(亏损以“-”号填列)        284,992,517.86
#:     四、利润总额(亏损总额以“-”号填列)     286,137,806.23
#:
#: 引号 `“”`（U+201C/D）、连字符 `-`（U+002D）、全角冒号 `：`（U+FF1A）
#: 和半角冒号 `:` 原先都**不在**字符类里 —— 匹配会在 `亏损以` 后面断掉，
#: 然后从 `号填列)` 重新开始匹配，**真科目名丢了**；
#: `strip_names` 认不出「号填列」这种残渣，于是**整行被静默丢掉**。
#:
#: 实测代价（某 A 股年报第 75 页）：`营业利润` 和 `利润总额` 两行全丢，
#: 连带 EBITDA 和实际税率都算不出来 —— 而这两行是 DCF 的必备输入。
#: ⚠️ 连字符必须写成 `\-`：放在 `‘’` 和 `—` 中间会被正则当成**区间** `’-—`，
#: `re.compile` 直接报 `bad character range`。
_LABEL_PUNCT = "（）()、·：:“”‘’\\-—／/《》〈〉"
_LABEL_CHARS = "\u4e00-\u9fff" + _LABEL_PUNCT

#: 一行最多允许几个金额。
#:
#: **不能只允许两个。** 实测：
#:
#:     A 股年报            本期金额 | 上期金额                      2 个
#:     招股说明书          2022H1 | 2021 | 2020 | 2019            4 个
#:     （带「行次」列）      行次 | 本期 | 上期                    3 个
#:
#: 只吃 3 个的时候，招股说明书的四个期间会被截断，而且取的是**最后两个**
#: —— 也就是**最旧的两期**（2020/2019），**静默取错期间**。
_MAX_AMOUNTS = 6
#: 最多取几期。招股说明书常见四期；多了下游也放不下，取最前面几期。
_MAX_PERIODS = 4
_PIECE = re.compile(
    r"([" + _LABEL_CHARS + r"][" + _LABEL_CHARS + r"]*)"    # 科目名串
    r"[\s\u3000]*"
    r"(" + _AMOUNT + r"(?:[\s\u3000]+" + _AMOUNT + r"){0," + str(_MAX_AMOUNTS - 1) + r"})"
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

        # **三个数以上时的处理。**
        # 情况一：第一个是「行次」（纯数字、无千分位、无小数点、值不大）→ 剥掉
        # 情况二：不是行次 → 就是多期数据（招股说明书常见四期）
        note = ""
        if len(numbers) >= 3:
            head = numbers[0]
            if head.isdigit() and len(head) <= 3 and int(head) <= _MAX_ROW_NUMBER:
                note, numbers = head, numbers[1:]
            else:
                numbers = numbers[:_MAX_PERIODS]

        label = names[0]
        # **取最前面两期。**
        #
        # 原先是 `[-2:]`（取最后两个）。对两期报表没差别，但**招股说明书是
        # 四期、由新到旧**（`2022年6月30日 | 2021 | 2020 | 2019`），
        # 取最后两个等于取了**最旧的两期**（2020/2019），
        # **静默取错期间** —— 数字本身还挺像样，不会被任何校验抓住。
        #
        # 中文报表的列一律由新到旧（本期/上期、期末/年初），所以取前面是对的。
        padded = (numbers + ["", ""])[:2]
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
