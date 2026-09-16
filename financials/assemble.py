"""按**内容**把三张表组装起来 —— 不看页码，不靠标题。

## 为什么换掉「页码定位」（实测：12 家公司里 8 家失败）

原来靠「标题在哪个页 + 往后取几页」。这条路走了三次补丁，每次修好一份、
弄坏另一份：

1. 按标题找 → 扫描件标题被 OCR 认烂时失手
   （国城矿业母公司资产负债表被认成「股名數公司燕苎鎮债表」，一个字都不对）
2. 改用目录 → 年报的目录格式又不一样
3. 加「编制单位」当起始标志 → 洛阳钼业直接从 (38,117) 开始

**根因**：位置和标题根本不足以确定「哪几页是这张表」。同一份材料里：

    紫金矿业 377 页      合并资产负债表 的标题出现在 4 / 108 / 110 / 117 / 118 五处
    洛阳钼业 252 页      标题在 123，跑到 150 页之后全是财报附注
    江西铜业 249 页      标题在 90

年报的财报附注有 200+ 页，表格密度远超正表，任何「按数值多少挑」的启发式
都会被附注带偏。

## 换成的做法

**让每一页自己说它属于哪张表。** 看这一页出现了哪些**标志性科目**：

    同时出现「资产总计」「负债合计」  → 这是资产负债表
    同时出现「营业收入」「净利润」    → 这是利润表
    同时出现「经营活动」「投资活动」  → 这是现金流量表

标志是按**组合**用的，不是单个词 —— 「净利润」单看会命中到处都在的附注，
但「营业收入 + 营业利润 + 净利润」同时出现在一页上，基本只可能是利润表本体。

然后把标签相同的**连续页**归成一张表。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ingest import pdf as ip

#: 每类报表的**标志组合**。
#: 外层元组是「必须至少命中一个」的组，组数越多分越高。
_MARKERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "balance": (
        ("资产总计", "資產總額", "负债合计", "負債總額", "所有者权益合计",
         "權益總額", "负债和所有者权益总计", "资产净额", "資產淨額"),
        ("流动资产合计", "非流动资产合计", "流动负债合计", "非流动负债合计",
         "流動資產淨額", "總資產減流動負債"),
        ("货币资金", "应收账款", "存货", "固定资产", "应付账款", "短期借款",
         "現金及銀行結餘", "貿易應收款項", "存貨", "物業、廠房及設備"),
    ),
    "income": (
        ("营业收入", "营业总收入", "产品销售收入", "营业收入合计",
         "营业额", "營業額", "收入", "收益"),
        ("营业利润", "利润总额", "净利润", "除税前溢利", "年度溢利",
         "本年度溢利", "淨利潤", "利潤總額", "年內溢利", "除稅前溢利"),
        ("营业成本", "税金及附加", "销售费用", "管理费用", "财务费用",
         "銷售成本", "行政開支", "融資成本", "銷售及分銷成本"),
    ),
    "cash_flow": (
        # **必须三条「净额」行同时出现。**
        # 原来只要求「经营活动」+「投资/筹资」两类词，太松：
        # 现金流量表在 MD&A 和附注里到处被提到，实测茅台被判到第 115 页
        # （财报附注），真表在第 64 页。
        # 附注的「现金流量表补充资料」只讲经营活动那条调节，
        # **不会同时出现投资和筹资的净额行** —— 这是可靠的区分点。
        ("经营活动产生的现金流量净额", "经营活动现金流量净额", "经营活动所用现金净额",
         "經營活動所得現金淨額", "經營活動所用現金淨額", "經營活動產生的現金流量淨額"),
        ("投资活动产生的现金流量净额", "投资活动现金流量净额", "投资活动所用的现金净额",
         "投資活動所得現金淨額", "投資活動所用現金淨額", "投資活動產生的現金流量淨額"),
        ("筹资活动产生的现金流量净额", "筹资活动现金流量净额", "融资活动所用的现金净额",
         "融資活動所得現金淨額", "融資活動所用現金淨額", "籌資活動產生的現金流量淨額"),
        # **必须有期初/期末现金那条滚存。**
        # 前面几页的「主要会计数据」「财务报表附注目录」会罗列三张表的名字，
        # 但没有这条滚存 —— 实测不加这一组时，茅台被判到第 10 页、
        # 蓝色光标到第 19 页、洛阳钼业到第 38 页（全是概览页）。
        ("期初现金及现金等价物余额", "年初现金及现金等价物", "年初的现金及现金等价物",
         "於年初之現金及現金等價物", "年初之現金及現金等價物"),
        ("期末现金及现金等价物余额", "年末现金及现金等价物", "年末的现金及现金等价物",
         "於年末之現金及現金等價物", "年末之現金及現金等價物"),
    ),
}


@dataclass
class PageScore:
    number: int
    scores: dict[str, int] = field(default_factory=dict)

    @property
    def best(self) -> str | None:
        if not self.scores:
            return None
        k = max(self.scores, key=lambda x: self.scores[x])
        return k if self.scores[k] > 0 else None


def signature(text: str) -> dict[str, int]:
    """这一页对每类报表的「证据分」。

    分 = 命中的**组数** × 100 + 命中的**科目数**。

    组数优先：一页同时出现「营业收入」和「净利润」比出现十个
    只有「净利润」的附注段落更有说服力。
    """
    out: dict[str, int] = {}
    for kind, groups in _MARKERS.items():
        g_hit = 0
        n_hit = 0
        for g in groups:
            hits = sum(1 for m in g if m in text)
            if hits:
                g_hit += 1
                n_hit += hits
        out[kind] = g_hit * 100 + n_hit if g_hit >= 2 else 0
    return out


def page_scores(doc: ip.PdfDocument) -> list[PageScore]:
    return [PageScore(number=pg.number, scores=signature(pg.text)) for pg in doc.pages]


def pick_pages(scores: list[PageScore], kind: str,
               max_gap: int = 0) -> list[int]:
    """挑出属于 `kind` 的连续页。

    1. 取分最高的那页当锚点；
    2. 往后走，只要后面某页对 `kind` 的分**高于**其他类就继续；
       遇到别的类的分明显更高就停。
    """
    anchor = None
    best = 0
    for s in scores:
        v = s.scores.get(kind, 0)
        if v > best:
            anchor, best = s.number, v
    if anchor is None:
        return []

    by_num = {s.number: s for s in scores}
    pages = [anchor]

    # 往后
    gap = 0
    n = anchor
    total = max(by_num) if by_num else anchor
    while n < total:
        n += 1
        s = by_num.get(n)
        if s is None:
            break
        mine = s.scores.get(kind, 0)
        other = max((v for k, v in s.scores.items() if k != kind), default=0)
        if mine >= other and mine > 0:
            pages.append(n)
            gap = 0
        elif mine == 0 and other == 0 and gap < max_gap:
            pages.append(n)                 # 中间的空白页 / 跨页续行
            gap += 1
        else:
            break

    # 往前（表可能从锚点前一页开始）
    n = anchor
    while n > 1:
        n -= 1
        s = by_num.get(n)
        if s is None:
            break
        mine = s.scores.get(kind, 0)
        other = max((v for k, v in s.scores.items() if k != kind), default=0)
        if mine > 0 and mine >= other:
            pages.insert(0, n)
        else:
            break

    return sorted(pages)


def statements_map(scores: list[PageScore]) -> dict[str, list[int]]:
    return {k: pick_pages(scores, k) for k in _MARKERS}
