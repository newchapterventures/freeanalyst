"""可比公司倍数 —— 把 EDGAR 财报和行情价格接起来。

## 这个模块在哪个域

它**跨越了机密域和公开域**，所以要说清楚：

- 它**不读**本地语料（corpus / root / 索引）
- 它向公开域发的请求参数只有市场、代码、日期、CIK —— 全是结构化值
- 它产出的结果（一组倍数）是**公开信息**，不含任何机密内容

所以它属于公开域，放在 `valuation/` 下只是因为它是估值流程的一环。
**读机密材料的模块（检索、文档解析）不要 import 它，除非确实需要公开数据。**

## 这一步在闭环里的位置

```
EDGAR 财报    →  EBITDA、收入、净利、总资产、股本、债务、现金
行情价格      →  指定基准日的收盘价
─────────────────────────────────────────────────────
市值 = 价格 × 股本
EV   = 市值 + 净债务 − 现金
倍数 = EV / EBITDA  或  EV / 收入  或  市值 / 净利
```

## 四条不能破的规则

**1. 基准日必须显式给。** 没有默认值。用哪天的价、哪期的表，是估值判断，
不是程序该替你决定的事。

**2. 财报期间和价格时点必须说清差多远。** EBITDA 是 FY2025 的（截至
2025-09-27），价格是 2025-12-31 的 —— 差三个月。这是行业惯例，但必须标出来，
不能默默糊过去。

**3. 算不出来就报缺口，不用行业均值填。** 缺 D&A 就不给 EBITDA 倍数，
缺净债务就不给 EV 倍数。**宁可少给一个数，不给一个错的数。**

**4. 股本要和价格同一时点。** 优先用最接近基准日的申报股本；
拿不到就标出来，不能拿两年前的股本配今天的价。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from datasources import prices
from datasources import sec_edgar as se

# 债务与现金的候选标签
_DEBT_TAGS = (
    "LongTermDebtNoncurrent",
    "LongTermDebt",
    "LongTermDebtCurrent",
    "DebtCurrent",
    "ShortTermBorrowings",
    "NotesPayableCurrent",
)

_CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "CashAndCashEquivalents",
)

# 股本
_SHARES_TAGS = (
    "EntityCommonStockSharesOutstanding",   # 在 dei 分类下，是申报日的股本
    "CommonStockSharesOutstanding",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
)


@dataclass
class CompsEntry:
    """一家可比公司的倍数。缺的项是 None，不是 0。"""

    entity: str
    cik: str
    fiscal_end: str            # EBITDA 对应的财报期末
    price_date: str | None     # 价格对应的交易日
    price: float | None
    shares: float | None
    market_cap: float | None
    net_debt: float | None
    enterprise_value: float | None
    ebitda: float | None
    revenue: float | None
    ebitda_margin: float | None

    # 计算依据，附在结果上让人核对
    tags: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    @property
    def ev_ebitda(self) -> float | None:
        if self.enterprise_value is None or not self.ebitda or self.ebitda <= 0:
            return None
        return self.enterprise_value / self.ebitda

    @property
    def ev_revenue(self) -> float | None:
        if self.enterprise_value is None or not self.revenue or self.revenue <= 0:
            return None
        return self.enterprise_value / self.revenue

    @property
    def price_earnings_note(self) -> str:
        return "P/E 需要净利润与摊薄股数，本版未接"


def _sum_latest(facts: dict, tags: tuple[str, ...], end: str) -> tuple[float, list[str]]:
    """把多个标签在指定期末的值加起来。

    债务和现金通常分散在多个科目（长期借款、一年内到期、短期借款…），
    所以要加总，不能只取一个。返回用到的标签名，便于核对。
    """
    total = 0.0
    used: list[str] = []
    for tag in tags:
        obs = se.value_for_period(facts, tag, end)
        if obs:
            total += obs.value
            used.append(tag)
    return total, used


def _shares_nearest(facts: dict, as_of: str) -> tuple[float | None, str, str | None]:
    """取最接近基准日的股本。

    返回 (股数, 用的标签, 股本对应日期)。
    拿不到就返回 (None, "未找到", None) —— **不要回退到别的口径**。
    """
    best: tuple[float, str, str] | None = None
    for taxonomy in ("dei", "us-gaap"):
        for tag in _SHARES_TAGS:
            series = se.extract_series(facts, tag, "shares", taxonomy)
            if not series:
                continue
            # 取不晚于 as_of 的最近一条
            eligible = [o for o in series if o.end <= as_of]
            if not eligible:
                continue
            obs = eligible[-1]
            if best is None or obs.end > best[2]:
                best = (obs.value, f"{taxonomy}:{tag}", obs.end)
    if best is None:
        return None, "未找到", None
    return best


def _first_filed_by_fiscal_end(facts: dict) -> dict[str, str]:
    """每个财年期末**首次**被申报的日期。

    实现已提到 `datasources/sec_edgar.py` 共用 —— 假设参谋（advisor）
    也要做同样的判断，两处必须用同一套口径。
    """
    return se.first_filed_by_fiscal_end(facts)


def _latest_reported_fiscal_end(facts: dict, as_of: str) -> str | None:
    """最近一个**在基准日之前已经披露**的财年期末。

    ## 为什么必须按披露日筛，而不能直接取最新的财年

    实测踩到的坑：Oracle 的财年期末是 2026-05-31，而基准日是 2025-12-31。
    直接取"最新财年"的话，会拿 2025 年 12 月的股价配 2026 年 5 月才结束的
    财报 —— **用了当时根本还不存在的数据。**

    这叫前视偏差（look-ahead bias）。它在回测里让收益率好得离谱，
    在真实决策里让你看到幻觉。

    正确的口径：站在基准日这一天，你只能看到那天之前**已经申报**的财报。
    """
    first_filed = _first_filed_by_fiscal_end(facts)
    eligible = [end for end, filed in first_filed.items() if filed <= as_of]
    return max(eligible) if eligible else None


def _fiscal_end_filed_after(facts: dict, fiscal_end: str, as_of: str) -> str | None:
    """检查指定财年是否在基准日之后才首次披露。是则返回首次披露日。"""
    filed = _first_filed_by_fiscal_end(facts).get(fiscal_end)
    return filed if (filed and filed > as_of) else None


def build_entry(
    cik: str,
    as_of: str,
    market: str,
    code: str,
    *,
    fiscal_end: str | None = None,
    shares_override: float | None = None,
) -> CompsEntry:
    """算一家可比公司的倍数。

    as_of        价格基准日（必填，无默认值）
    fiscal_end   财报期末。不填就用**基准日之前已披露**的最近财年。

    **不填 fiscal_end 是对的用法**，它会自动避开前视偏差。
    显式指定的话，如果那个财年在基准日之后才披露，会给出警告。
    """
    facts = se.company_facts(cik)
    entity = facts.get("entityName", f"CIK {cik}")
    tags: dict[str, str] = {}
    gaps: list[str] = []

    # ---- 财报期末 ----
    fe = fiscal_end
    if fe is None:
        fe = _latest_reported_fiscal_end(facts, as_of)
        if fe is None:
            gaps.append(f"截止 {as_of} 找不到任何已披露的完整财年 —— "
                        f"可能是新上市公司，或该财年还没出年报")
            return CompsEntry(entity, cik, "", None, None, None, None, None, None,
                              None, None, None, tags, gaps)
    else:
        late = _fiscal_end_filed_after(facts, fe, as_of)
        if late:
            gaps.append(
                f"指定财年 {fe} 是 {late} 才披露的，晚于基准日 {as_of} —— "
                f"这是前视偏差：当时你还看不到这份财报"
            )
    tags["fiscal_end"] = fe
    tags["fiscal_end_basis"] = f"截止 {as_of} 已披露的最近财年"

    # ---- 财报侧 ----
    rev = se.derive_revenue(facts, fe)
    eb = se.derive_ebitda(facts, fe)
    if rev:
        tags["revenue"] = rev.tag
    else:
        gaps.append(f"缺营业收入（期末 {fe}）")
    if eb:
        tags["ebitda"] = eb.tag
    else:
        gaps.append(f"缺 EBITDA（期末 {fe}）—— 通常是缺折旧摊销科目。"
                    f"**不用行业均值填充。**")

    debt, debt_tags = _sum_latest(facts, _DEBT_TAGS, fe)
    cash, cash_tags = _sum_latest(facts, _CASH_TAGS, fe)
    if debt_tags:
        tags["debt"] = " + ".join(debt_tags)
    else:
        gaps.append("缺有息负债科目，净债务无法算 → EV 也算不了")
    if cash_tags:
        tags["cash"] = " + ".join(cash_tags)
    else:
        gaps.append("缺现金科目")

    # ---- 价格侧 ----
    bar = prices.close_on(market, code, as_of)
    price_date = price = None
    if bar is None:
        gaps.append(f"{market}/{code} 取不到不晚于 {as_of} 的收盘价")
    else:
        price, price_date = bar.close, bar.date
        tags["price"] = f"东财日线 {bar.secid} fqt=0"

    # ---- 股本 ----
    if shares_override is not None:
        shares, shares_tag, shares_date = shares_override, "手工指定", as_of
    else:
        shares, shares_tag, shares_date = _shares_nearest(facts, as_of)
    if shares is None:
        gaps.append("缺股本数据，市值算不了")
    else:
        tags["shares"] = shares_tag
        if shares_date and price_date and shares_date < fe:
            gaps.append(f"股本日期 {shares_date} 早于财报期末 {fe}，"
                        f"两者口径可能不一致")

    # ---- 组装 ----
    market_cap = price * shares if (price and shares) else None
    net_debt = (debt - cash) if (debt_tags and cash_tags) else None
    ev = (market_cap + net_debt) if (market_cap is not None and net_debt is not None) else None

    ebitda_val = eb.value if eb else None
    rev_val = rev.value if rev else None
    margin = (ebitda_val / rev_val) if (ebitda_val and rev_val) else None

    entry = CompsEntry(
        entity=entity, cik=cik, fiscal_end=fe,
        price_date=price_date, price=price, shares=shares,
        market_cap=market_cap, net_debt=net_debt, enterprise_value=ev,
        ebitda=ebitda_val, revenue=rev_val, ebitda_margin=margin,
        tags=tags, gaps=gaps,
    )

    # 时点差异必须标出来
    if price_date and price_date > fe:
        from datetime import date
        try:
            d = (date.fromisoformat(price_date) - date.fromisoformat(fe)).days
            if d > 120:
                entry.gaps.append(
                    f"价格日期 {price_date} 比财报期末 {fe} 晚 {d} 天，"
                    f"期间可能有重大变化"
                )
        except ValueError:
            pass
    return entry


@dataclass
class CompsSummary:
    """一组可比公司的倍数分布。"""

    entries: list[CompsEntry]
    metric: str

    def values(self) -> list[float]:
        out = []
        for e in self.entries:
            v = getattr(e, self.metric, None)
            if v is not None and v == v and v != float("inf"):
                out.append(v)
        return sorted(out)

    def quantile(self, q: float) -> float | None:
        vals = self.values()
        if not vals:
            return None
        if len(vals) == 1:
            return vals[0]
        pos = q * (len(vals) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(vals) - 1)
        frac = pos - lo
        return vals[lo] * (1 - frac) + vals[hi] * frac

    def median(self) -> float | None:
        return self.quantile(0.5)

    def render(self) -> str:
        vals = self.values()
        if not vals:
            return f"{self.metric}：无可用数据"
        lines = [
            f"{self.metric}  样本 {len(vals)} 家",
            f"  最小 {vals[0]:>8.2f}x",
            f"  25%  {self.quantile(0.25):>8.2f}x",
            f"  中位 {self.median():>8.2f}x",
            f"  75%  {self.quantile(0.75):>8.2f}x",
            f"  最大 {vals[-1]:>8.2f}x",
        ]
        return "\n".join(lines)
