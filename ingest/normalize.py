"""文本归一化 —— 零依赖，纯标准库。

## 为什么需要这个（这是实测踩到的坑，不报错的那种）

PDF 里抽出来的中文，字符码位经常不是你以为的那个。实测：

    抽出： 货币资⾦    codepoints  ... 0x2fa6
    正确： 货币资金    codepoints  ... 0x91d1

`⾦` 是 **U+2FA6 康熙部首金**，不是 **U+91D1 汉字金**。它们看着一样，
`==` 比较是 `False`。

**后果是静默的**：科目名对不上、检索命不中、BM25 查不到 ——
而且不报错。整条流水线看起来在跑，结果是错的。

一份 1079 字的测试财报里，有 **16 个不同字符**受影响：

    康熙部首  ⼈ ⼊ ⼝ ⼯ ⽀ ⽐ ⽑ ⽣ ⽤ ⽬ ⾦ ⾮
    全角标点  （ ） ， ：

## 用什么修

**第一步**：`unicodedata.normalize("NFKC", s)`。NFC/NFD 不管这个 ——
康熙部首的映射属于 **compatibility** 分解，只有 NFKC/NFKD 会做。

**第二步（NFKC 修不了的那些）**：
CJK 部首补充区（U+2E80–U+2EFF）里**有 113 个字符没有 NFKC 映射**。
实测一份财报里就漏了一个：

    ⻓期借款      ⻓ 是 U+2ED3（CJK RADICAL C-SIMPLIFIED LONG）
                 不是 长 U+957F，而且 NFKC 不动它

其中 30 个是简体部首形，**会真实出现在中文正文里**（长/门/车/风/页/马…），
所以下面手工补了一张映射表。剩下的 83 个是罕见部首，中文里基本不出现 ——
**遇到就报告出来，不猜**（猜错比不做更糟）。

## 两个必须守住的点

**① 归一化只能在一处做。**
   索引侧和查询侧口径不一致，就会静默失配 —— 比不归一化还隐蔽。
   所有进出文本都过这个模块。

**② 归一化改变不了「数字」和「事实」。**
   NFKC 会把全角 `％` 变成 `%`、`①` 变成 `1`、`㎡` 变成 `m2`。
   这些是**排版变体**，不是新信息 —— 不影响证据规则里
   「数字原样引用」的要求（那说的是不许改数值和单位）。
   但如果将来要逐字引用原文给用户看，**要保留归一化前的副本**。
"""

from __future__ import annotations

import unicodedata

#: CJK 部首补充区里 **NFKC 修不了、但会作为正字出现在中文正文里** 的字符。
#
# ## 这批映射是怎么来的
#
# 一份合成的中文财报 PDF 实测漏了两个：
#     ⻓期借款 → ⻓ U+2ED3      民 → ⺠ U+2EA0
# 第一个在"SIMPLIFIED"那批里，第二个**不在** —— 它名字里只有 CIVILIAN。
#
# **所以"靠名字里有 SIMPLIFIED 来筛"这个做法是错的**，我第一版就是这么筛的，
# 结果漏了 民 这个再常见不过的字。
#
# 现在按另一条标准筛：**这个部首形本身就是某个汉字的完整字形**。
# 像 亻/氵/扌/衤 这类只作偏旁、不作正字的，不在此表 ——
# 它们出现在正文里纯属字体编码事故，**报出来让人判断，不猜**。
#
# ## 逐条的核对方式
#
# 源字符的 Unicode 名称给出含义（CIVILIAN/SUN/EYE…），
# 目标字符必须就是该含义对应的那个字。**不靠字形猜。**
_RADICAL_FIX = {
    # ---- 简体部首形（源字名含 SIMPLIFIED）----
    "\u2ea6": "丬",   # SIMPLIFIED HALF TREE TRUNK → 丬
    "\u2eb0": "纟",   # C-SIMPLIFIED SILK              → 纟
    "\u2ec5": "见",   # C-SIMPLIFIED SEE               → 见
    "\u2ec6": "角",   # SIMPLIFIED HORN                → 角
    "\u2ec8": "讠",   # C-SIMPLIFIED SPEECH            → 讠
    "\u2ec9": "贝",   # C-SIMPLIFIED SHELL             → 贝
    "\u2ecb": "车",   # C-SIMPLIFIED CART              → 车
    "\u2ecc": "辶",   # SIMPLIFIED WALK                → 辶
    "\u2ed0": "钅",   # C-SIMPLIFIED GOLD              → 钅
    "\u2ed3": "长",   # C-SIMPLIFIED LONG              → 长  ← 实测踩到
    "\u2ed4": "门",   # C-SIMPLIFIED GATE              → 门
    "\u2ed9": "韦",   # C-SIMPLIFIED LEATHER           → 韦
    "\u2eda": "页",   # C-SIMPLIFIED LEAF              → 页
    "\u2edb": "风",   # C-SIMPLIFIED WIND              → 风
    "\u2edc": "飞",   # C-SIMPLIFIED FLY               → 飞
    "\u2ee0": "饣",   # C-SIMPLIFIED EAT               → 饣
    "\u2ee2": "马",   # C-SIMPLIFIED HORSE             → 马
    "\u2ee5": "鱼",   # C-SIMPLIFIED FISH              → 鱼
    "\u2ee6": "鸟",   # C-SIMPLIFIED BIRD              → 鸟
    "\u2ee7": "卤",   # C-SIMPLIFIED SALT              → 卤
    "\u2ee8": "麦",   # SIMPLIFIED WHEAT               → 麦
    "\u2ee9": "黄",   # SIMPLIFIED YELLOW              → 黄
    "\u2eea": "黾",   # C-SIMPLIFIED FROG              → 黾
    "\u2eeb": "齐",   # J-SIMPLIFIED EVEN              → 齐
    "\u2eec": "齐",   # C-SIMPLIFIED EVEN              → 齐
    "\u2eed": "齿",   # J-SIMPLIFIED TOOTH             → 齿
    "\u2eee": "齿",   # C-SIMPLIFIED TOOTH             → 齿
    "\u2eef": "龙",   # J-SIMPLIFIED DRAGON            → 龙
    "\u2ef0": "龙",   # C-SIMPLIFIED DRAGON            → 龙
    "\u2ef2": "龟",   # J-SIMPLIFIED TURTLE            → 龟

    # ---- 部首形即正字（源字名只有含义，没有 SIMPLIFIED）----
    "\u2e9c": "日",   # SUN        → 日
    "\u2e9d": "月",   # MOON       → 月
    "\u2e9e": "歹",   # DEATH      → 歹
    "\u2ea0": "民",   # CIVILIAN   → 民  ← 实测踩到
    "\u2ea3": "火",   # FIRE       → 火
    "\u2ea7": "牛",   # COW        → 牛
    "\u2ea8": "犬",   # DOG        → 犬
    "\u2ea9": "玉",   # JADE       → 玉
    "\u2eaa": "疋",   # BOLT OF CLOTH → 疋
    "\u2eab": "目",   # EYE        → 目
    "\u2eae": "竹",   # BAMBOO     → 竹
    "\u2eaf": "糸",   # SILK       → 糸
    "\u2eb6": "羊",   # SHEEP      → 羊
    "\u2eb9": "老",   # OLD        → 老
    "\u2ebd": "臼",   # MORTAR     → 臼
    "\u2ec1": "虎",   # TIGER      → 虎
    "\u2ec4": "西",   # WEST TWO   → 西
    "\u2ec7": "角",   # HORN       → 角
    "\u2eca": "足",   # FOOT       → 足
    "\u2ecf": "邑",   # CITY       → 邑
    "\u2ed7": "雨",   # RAIN       → 雨
    "\u2ed8": "青",   # BLUE       → 青
    "\u2ee3": "骨",   # BONE       → 骨
    "\u2ee4": "鬼",   # GHOST      → 鬼
    "\u2ef1": "龟",   # TURTLE     → 龟
}

_RADICAL_TABLE = str.maketrans(_RADICAL_FIX)


def normalize_text(s: str) -> str:
    """把文本归一化成可比较的形式。

    两步：NFKC 处理兼容性映射，再查表修 CJK 部首（NFKC 管不到那一段）。

    幂等：`normalize(normalize(x)) == normalize(x)` —— 这条很重要，
    否则反复入库会让文本漂移。
    """
    if not s:
        return s
    return unicodedata.normalize("NFKC", s).translate(_RADICAL_TABLE)


def compat_issues(s: str) -> list[str]:
    """列出这段文本里**看着一样但码位不同**的字符。

    用于入库时报告「这份材料的文字有过归一化处理」，
    以及在排查检索不到时快速确认是不是这个原因。
    """
    out: set[str] = set()
    for ch in s:
        if ch in _RADICAL_FIX:
            out.add(ch)
        elif unicodedata.normalize("NFKC", ch) != ch:
            out.add(ch)
    return sorted(out)


def unfixable_issues(s: str) -> list[str]:
    """这段文本里**看着可疑但没有映射、我拒绝猜**的字符。

    CJK 部首补充区剩下的 83 个字符属于这一类。中文正文里基本不出现，
    一旦出现，报出来让人判断 —— **猜错比不做更糟。**
    """
    out: set[str] = set()
    for ch in s:
        if ch in _RADICAL_FIX:
            continue
        if unicodedata.normalize("NFKC", ch) != ch:
            continue
        cp = ord(ch)
        if 0x2E80 <= cp <= 0x2EFF or 0x2F00 <= cp <= 0x2FDF:
            out.add(ch)
    return sorted(out)


def describe_compat_issues(s: str, limit: int = 12) -> str:
    issues = compat_issues(s)
    parts: list[str] = []
    if issues:
        shown = issues[:limit]
        detail = "  ".join(f"{c}(U+{ord(c):04X})" for c in shown)
        more = f"  …还有 {len(issues) - limit} 个" if len(issues) > limit else ""
        parts.append(
            f"{len(issues)} 个兼容性字符已归一化：{detail}{more}\n"
            f"（这些字符看着和正常汉字一样，但 `==` 比较不相等）"
        )
    unknown = unfixable_issues(s)
    if unknown:
        detail = "  ".join(f"{c}(U+{ord(c):04X})" for c in unknown[:limit])
        parts.append(
            f"⚠ {len(unknown)} 个字符在部首区但**没有映射**：{detail}\n"
            f"（我拒绝猜它们的正字 —— 猜错比不处理更糟。请人工确认）"
        )
    return "\n".join(parts) if parts else "无兼容性字符"
