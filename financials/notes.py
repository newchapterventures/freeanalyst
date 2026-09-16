"""从**附注**里取估值要的字段 —— 目前是折旧摊销（D&A）。

## 为什么必须做这一步（实测）

估值引擎的 `DcfInputs` 要 `da_pct_revenue`，倍数法要 EBITDA，
而 `financials/derive.py` 里 `ebitda()` / `net_debt()` 早就写好了 ——
**但链子从源头就断了：D&A 抽不到。**

实测三家材料，D&A 全是空：

    某白酒公司       ✗    某非上市公司   ✗    某 H 股公司   ✗

原因不是材料没有，是**我只读了三张表的主表页**。D&A 在**附注**里：

    某白酒公司 第 115 页「补充资料」—— 现金流量表补充资料（间接法调节段）
    某非上市公司 第 31 页

    1.将净利润调节为经营活动现金流量:
      净利润 85,310,324,833.67
      加:资产减值准备信用减值损失 -17,234,379.37
      固定资产折旧、油气资产折耗、生产性生物资产折旧 1,893,338,311.91
      使用权资产摊销 55,797,324.89
      无形资产摊销 289,613,682.99
      长期待摊费用摊销 20,637,734.49

**A 股主表没有间接法段**（`articulation` 一直在报「不适用」，就是这个原因），
调节表只在补充资料里。所以估值需要的字段，**必须从附注取**。

## 解析难点：数字插在标签中间

和主表一样，文字层会把数字塞进标签里：

    固定资产折旧、油气资产折耗、生产 1,893,338,311.91 1,721,165,327.14 性生物资产折旧

完整标签不存在。所以**用前缀定位，再取它后面第一个金额**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ingest import pdf as ip

#: 间接法调节段的标志 —— 中英港三套写法
_RECON_MARKERS = (
    "将净利润调节为经营活动现金流量",
    "将净利润调节为经营活动产生的现金流量",
    "净利润调节为经营活动现金流量",
    "Adjustments for:",
    "Reconciliation of profit",
    "Profit before taxation adjusted for",
)

#: 折旧摊销的各组成部分。
#: 值 = 用来定位的**前缀**（短，不会被数字切断），键 = 展示名。
#: ⚠️ 顺序有意义：长的、更具体的放前面 —— 「固定资产折旧」会命中
#: 「固定资产折旧、油气资产折耗、生产性生物资产折旧」这一整行，是对的；
#: 但反过来就错了。
_DA_PREFIXES: tuple[tuple[str, str], ...] = (
    ("固定资产折旧", "固定资产折旧"),
    ("投资性房地产折旧", "投资性房地产折旧"),
    ("使用权资产摊销", "使用权资产摊销"),
    ("无形资产摊销", "无形资产摊销"),
    ("长期待摊费用摊销", "长期待摊费用摊销"),
    ("油气资产折耗", "油气资产折耗"),
    ("生产性生物资产折旧", "生产性生物资产折旧"),
    ("Depreciation of property", "固定资产折旧"),
    ("Depreciation of right-of-use", "使用权资产摊销"),
    ("Depreciation of investment properties", "投资性房地产折旧"),
    ("Amortisation of intangible", "无形资产摊销"),
    ("Amortization of intangible", "无形资产摊销"),
)

#: 金额 —— 带千分位和小数，或括号负数
_AMOUNT = re.compile(r"\(?-?[\d,]+(?:\.\d+)?\)?")
#: 数字插在标签里时，标签会被截成「…固定资产折旧、油气资产折耗、生产」
#: 这种样子，末尾是一个中文字。允许前缀后面跟**少量非数字字**再出现金额。


@dataclass
class DepreciationAmortisation:
    """从附注里抽出来的折旧摊销。"""

    components: dict[str, float] = field(default_factory=dict)
    pages: list[int] = field(default_factory=list)
    #: 抽到的原文行，便于人工复核
    evidence: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def total(self) -> float | None:
        return sum(self.components.values()) if self.components else None

    def render(self, unit: str = "") -> str:
        if not self.components:
            return f"  折旧摊销：**未取到**（{self.note or '附注里没找到调节段'}）"
        out = [f"  折旧摊销合计：**{self.total:,.2f}**{unit}"
               f"（取自第 {'、'.join(str(p) for p in self.pages)} 页附注）"]
        for k, v in self.components.items():
            out.append(f"      {k}  {v:,.2f}")
        return "\n".join(out)


def _to_number(s: str) -> float | None:
    t = s.strip().replace(",", "").replace(" ", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def find_reconciliation_pages(doc: ip.PdfDocument) -> list[int]:
    """哪几页有「将净利润调节为经营活动现金流量」这一段。"""
    return [pg.number for pg in doc.pages
            if any(m in pg.text for m in _RECON_MARKERS)]


def extract_da(doc: ip.PdfDocument) -> DepreciationAmortisation:
    """从补充资料里抽折旧摊销。

    ## 只认**调节段里**的那一行

    折旧摊销在年报里出现很多次（管理费用明细、固定资产附注…），
    但**只有间接法调节段里的那一行是「本期计提的 D&A 总额」**。

    实测某白酒公司第 109 页的「管理费用明细」里也有 `固定资产折旧费用 659,934,804.32`，
    那只是**计入管理费用的那部分**，不是全部。拿它当 D&A 会严重低估
    （真实的固定资产折旧是 1,893,338,311.91），**而且不会报错**。

    所以：先定位调节段，只在调节段那几页里找。
    """
    out = DepreciationAmortisation()
    pages = find_reconciliation_pages(doc)
    if not pages:
        out.note = "全文找不到「将净利润调节为经营活动现金流量」调节段"
        return out
    out.pages = pages

    # **按页码查表，不要拿页码当索引。** `doc.pages[n-1]` 只在页码
    # 从 1 连续编号时成立 —— 实测（假文档 + 未来可能的非连续页码）
    # 会直接 IndexError，把「取不到数」变成「崩」。
    by_number = {pg.number: pg for pg in doc.pages}

    for n in pages:
        pg = by_number.get(n)
        if pg is None:
            continue
        text = pg.text
        # **按数字的位置去重。** 源文里有一行挂三个标签的写法：
        #     固定资产折旧、油气资产折耗、生产性生物资产折旧 1,893,338,311.91
        # 三个前缀都会去取它后面第一个金额，取到同一个数 —— 重复计数，
        # 实测把某白酒公司的 D&A 从 22.6 亿算成 41.5 亿（虚增近一倍），而且不报错。
        claimed: set[int] = set()
        for prefix, label in _DA_PREFIXES:
            idx = text.find(prefix)
            if idx < 0:
                continue
            after = text[idx + len(prefix):]
            m = _AMOUNT.search(after)
            if not m:
                continue
            pos = idx + len(prefix) + m.start()
            if pos in claimed:
                continue
            v = _to_number(m.group(0))
            if v is None or v == 0:
                continue
            claimed.add(pos)
            if label in out.components:
                continue
            out.components[label] = v
            snippet = (prefix + after[:m.end()]).replace("\n", " ")
            out.evidence.append(f"第{n}页: {snippet[:90]}")

    if not out.components:
        out.note = f"找到调节段（第 {pages} 页）但没解析出折旧摊销行"
    return out
