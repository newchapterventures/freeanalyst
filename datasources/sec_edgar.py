"""SEC EDGAR 数据源 —— 美国上市公司的官方结构化财务数据。

## 为什么这个源特别值

- **完全免费**，不需要 API key，不需要注册
- **官方结构化数据**（XBRL），不用 LLM 去"读"年报
- `frames` 端点能一次拿到**同一科目、同一期间、所有申报人**的值 ——
  这就是可比公司抓取的现成答案

商业数据商在卖的很多东西，本质就是转卖这些。

## 两条硬约束

1. **必须带 User-Agent 且含联系方式** —— 不带直接 403（已实测）
2. **限速 10 请求/秒** —— 超过会被限流

## 数据边界（重要）

EDGAR 有财报，**没有股价**。所以：

| 能算 | 不能算 |
|---|---|
| EBITDA、收入、净利、总资产 | 市值 |
| 每股收益、股数 | EV（需要市值） |
| 跨公司同一科目的横向对比 | EV/EBITDA（需要 EV） |

要做市值的倍数，必须再配一个价格源。这一层设计成可插拔，见 `base.py`。
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import net

BASE = "https://data.sec.gov"
WWW = "https://www.sec.gov"

# SEC 要求 User-Agent 里带邮箱，否则直接 403。实测：
#     只写项目 URL     → 403
#     带邮箱           → 200
#     完全不带         → 403
#
# **不要把邮箱写死在这里** —— 这份代码是公开的，写死的邮箱会成为爬虫的猎物，
# 而且它也不代表用的人是谁。使用者自己设：
#
#     export SEC_USER_AGENT="你的机构名 你的邮箱@example.com"
ENV_KEY = "SEC_USER_AGENT"

# SEC 建议不超过 10 请求/秒
_MIN_INTERVAL = 0.12
_last_call = [0.0]

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "sec"
CACHE_TTL = 24 * 3600  # 一天

#: 这个源覆盖的市场与币种 —— **由源自己声明**，调用方不许写死。
#: （中/港的源接进来后，市场层靠这两个字段判断"能不能同台比"。）
MARKET = "us"
CURRENCY = "USD"

#: 这个源**有没有行情（价格/股数/市值）**。
#: EDGAR 是**申报系统**：它收录公司自己报的财报，但**没有股价** ——
#: 所以市值 / EV / EV·EBITDA 这类倍数在 EDGAR 上是**算不出来**的，不是"暂时没做"。
#: 写成字段而不是一句说明，是为了让"为什么给不了倍数"这个答案
#: **由源自己提供**（接了价格源的源把这个改成 True，接口那边不用改）。
HAS_PRICES = False


class SecEdgarError(RuntimeError):
    pass


def user_agent() -> str:
    """取 User-Agent，缺邮箱就报错并给出可操作的指引。

    **为什么不在代码里放一个默认邮箱**：这份代码是公开的，写死的邮箱会成为
    爬虫的猎物；而且它也不代表真正在用的人是谁 —— SEC 要的是"出问题能联系到谁"。

    所以宁可报错，也不要静默失败。SEC 的 403 不会有任何解释，
    使用者会以为是自己网络的问题。
    """
    ua = os.environ.get(ENV_KEY, "").strip()
    if "@" not in ua:
        raise SecEdgarError(
            "SEC EDGAR 要求 User-Agent 里含邮箱，否则一律返回 403。\n"
            "（实测：只写项目地址也会被拒，且 SEC 不解释原因。）\n\n"
            "设一下环境变量再运行：\n"
            f'    export {ENV_KEY}="你的机构名 你的邮箱@example.com"\n\n'
            "想永久生效就写进 ~/.zshrc。这个地址会出现在发给 SEC 的请求头里，\n"
            "所以要填真实的联系方式 —— 如果 SEC 那边有异常会联系你。"
        )
    return ua


def _throttle() -> None:
    delta = time.time() - _last_call[0]
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _last_call[0] = time.time()


def _maybe_gunzip(raw: bytes) -> bytes:
    """按魔数判断是否 gzip 并解压。

    不依赖 Content-Encoding 响应头 —— 请求 gzip 却不解压是最常见的坑，
    而 gzip 的魔数（1f 8b）足够可靠。
    """
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _cached_get(url: str, purpose: str, use_cache: bool = True) -> dict:
    """带磁盘缓存的 GET。缓存放本地 —— 减少对外请求，也减少暴露面。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = urllib.parse.quote(url, safe="")
    path = CACHE_DIR / f"{key[:180]}.json"

    if use_cache and path.exists():
        age = time.time() - path.stat().st_mtime
        if age < CACHE_TTL:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

    _throttle()
    raw = net.guarded_get(
        url,
        net.PublicQuery({"purpose_tag": purpose}, purpose=purpose),
        timeout=60,
        headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"},
    )
    data = json.loads(_maybe_gunzip(raw).decode("utf-8"))
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _normalize_cik(cik: str | int) -> str:
    """CIK 必须是 10 位、前导补零。"""
    digits = str(cik).strip().lstrip("0") or "0"
    if not digits.isdigit():
        raise SecEdgarError(f"CIK 必须是数字，收到 {cik!r}")
    return digits.zfill(10)


# ---------------------------------------------------------------------------
# 公司财务数据
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """一条 XBRL 数据点。保留 accession number 以便回溯到原始文件。"""

    start: str | None
    end: str
    value: float
    form: str
    fy: int | None
    fp: str | None
    filed: str
    accn: str


def company_facts(cik: str | int, use_cache: bool = True) -> dict:
    """一家公司全部 XBRL 标记事实。

    数据量大（Apple 约 8MB）。返回结构：
        facts[taxonomy][tag]["units"][unit] = [观察值…]
    """
    url = f"{BASE}/api/xbrl/companyfacts/CIK{_normalize_cik(cik)}.json"
    return _cached_get(url, purpose=f"companyfacts cik={cik}", use_cache=use_cache)


def extract_series(
    facts: dict,
    tag: str,
    unit: str = "USD",
    taxonomy: str = "us-gaap",
    duration: int | str | None = None,
) -> list[Observation]:
    """抽出一个科目的时间序列。

    **duration 参数是必须的（否则数据会错）**：同一个标签、同一个期末，
    XBRL 里会有多个数据点，区别在 start→end 的时长。年度值和季度值
    混在一起看，会把季度值当成年度值。

        duration="annual"   330–400 天
        duration="quarter"  80–100 天
        duration=None       全部（时点科目如总资产用这个，它们没有 start）
    """
    node = facts.get("facts", {}).get(taxonomy, {}).get(tag)
    if not node:
        return []
    rows = node.get("units", {}).get(unit, [])

    lo, hi = _duration_bounds(duration)

    out: list[Observation] = []
    for r in rows:
        if "val" not in r or "end" not in r:
            continue
        start = r.get("start")
        if lo is not None and hi is not None:
            if not start:
                continue  # 时点科目没有区间，不符合时长筛选
            span = _days_between(start, r["end"])
            if span is None or span < lo or span > hi:
                continue
        out.append(Observation(
            start=start, end=r["end"], value=float(r["val"]),
            form=r.get("form", ""), fy=r.get("fy"), fp=r.get("fp"),
            filed=r.get("filed", ""), accn=r.get("accn", ""),
        ))

    out.sort(key=lambda o: (o.end, o.filed))
    return out


def _duration_bounds(duration: int | str | None) -> tuple[int | None, int | None]:
    """时长筛选的边界。

    **XBRL 里的期间不止年度和季度两种**：上市公司的季报会给
    3 个月、6 个月、9 个月三种年内累计值（YTD）。只按"年度/季度"两档筛，
    9 个月的数据会两边都不落，等于凭空消失。
    """
    if duration is None:
        return None, None
    if duration == "annual":
        return 330, 400
    if duration == "quarter":
        return 80, 100
    if duration == "half":
        return 170, 190
    if duration == "ytd9":  # 三季报的年内累计
        return 260, 285
    if duration == "ytd":
        return 80, 330  # 任何年内区间，排除年度
    if isinstance(duration, int):
        return duration - 15, duration + 15
    raise ValueError(f"不认识的 duration：{duration!r}（用 'annual' / 'quarter' / 'half' / 'ytd9' / 'ytd' / 天数）")


def _days_between(start: str, end: str) -> int | None:
    from datetime import date
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except (ValueError, TypeError):
        return None
    return (e - s).days


def annual_series(facts: dict, tag: str, unit: str = "USD",
                  taxonomy: str = "us-gaap", dedupe: bool = True) -> list[Observation]:
    """年度序列 —— 排除掉同期的季度值。

    同一期末常有多条记录（不同申报日、或后续财报重述）。dedupe=True 时
    每个期末只保留**最晚申报**的那条。

    **重述是有意义的信号**，想看到全部就把 dedupe 关掉 ——
    两个版本都留着，可以看出公司改过什么。

    ⚠️ 注意：这里保留的是**最晚**申报。判断「这份财报在某个日期可不可见」
    不能用它 —— 每个新财年的 10-K 都会重述前两年的对照数，
    于是历史财年的 `filed` 会被推到很晚。那种判断要用
    `first_filed_by_fiscal_end()`。
    """
    series = extract_series(facts, tag, unit, taxonomy, duration="annual")
    if not dedupe:
        return series
    by_end: dict[str, Observation] = {}
    for o in series:
        prev = by_end.get(o.end)
        if prev is None or o.filed > prev.filed:
            by_end[o.end] = o
    return sorted(by_end.values(), key=lambda o: o.end)


def instant_series(facts: dict, tag: str, unit: str = "USD",
                   taxonomy: str = "us-gaap", dedupe: bool = True) -> list[Observation]:
    """**时点**序列 —— 资产负债表科目用这个。

    ## 为什么必须有这个函数（实测踩到的）

    资产负债表科目（总资产、货币资金、应收账款…）在 XBRL 里是**时点值**，
    只有 `end` 没有 `start`。

    而 `annual_series` 按**时长**筛选（330–400 天），时点科目没有 duration，
    于是一条都取不到 —— **27 个科目全部返回空，但不报错**。

    实测场景：拿 HTML 解析出的资产负债表跟官方 XBRL 对账，
    27 项全是「官方数据里没有」。差点以为是 HTML 解析错了，
    其实是取数函数用错了。

    区分规则很简单：
        有 start、有 end  → 区间科目（利润表、现金流量表）→ annual_series
        只有 end          → 时点科目（资产负债表）      → instant_series
    """
    series = [o for o in extract_series(facts, tag, unit, taxonomy, duration=None)
              if o.start is None]
    if not dedupe:
        return series
    by_end: dict[str, Observation] = {}
    for o in series:
        prev = by_end.get(o.end)
        # 时点值同样有重述：保留最晚申报的那条（和 annual_series 一致）
        if prev is None or o.filed > prev.filed:
            by_end[o.end] = o
    return sorted(by_end.values(), key=lambda o: o.end)


def instant_on(
    facts: dict,
    tag: str,
    as_of: str,
    unit: str = "USD",
    taxonomy: str = "us-gaap",
    filed_by: str | None = None,
) -> float | None:
    """某个科目在某个时点（期末）的值。找不到返回 None —— **不猜**。

    ## `filed_by`：核对一份具体申报文件时必须用

    `companyfacts` 返回的是**某个日期的最新视图**，不是**当时那一版**。

    实测（Fitbit FY2016 10-K）：

        期末 2016-12-31  Total assets
          该年报原文（2017-03-01 申报）      1,820,226
          companyfacts                      1,821,926
                       申报日 2018-03-01  ← FY2017 年报重述过

    两者差 1,700（千美元）。拿 companyfacts 直接去核对年报，
    会看到一堆"不符"，**而且差异长得很像自己的解析错误**。

    给定 `filed_by`（该申报文件的申报日）后，只采用当时已公开的数据，
    得到的就是**那份文件自己写的数字**。
    """
    best: Observation | None = None
    # **必须用未去重的序列。**
    # `instant_series` 默认按期末去重、只留最晚申报 ——
    # 于是年报自己那条（早申报的）会被后续年报的重述挤掉，
    # 再加 `filed_by` 过滤就什么都剩不下，代码会落到"退而求其次"分支，
    # 静静返回上一个季度的数（实测拿到 2016-10-01 的资产负债表）。
    for o in instant_series(facts, tag, unit, taxonomy, dedupe=False):
        if o.end != as_of:
            continue
        if filed_by is not None and o.filed > filed_by:
            continue
        if best is None or o.filed > best.filed:
            best = o
    if best is not None:
        return best.value

    if filed_by is not None:
        # 限定披露日时**不做跨期回退** —— 回退会静默给出另一个日期的数，
        # 而那个数看着完全正常。宁可返回 None。
        return None

    # 该期末没有精确匹配（比如财年日期不齐），退回「不晚于 as_of 的最近一期」
    for o in reversed(instant_series(facts, tag, unit, taxonomy)):
        if o.end <= as_of:
            return o.value
    return None


def annual_on(
    facts: dict,
    tag: str,
    end: str,
    unit: str = "USD",
    taxonomy: str = "us-gaap",
    filed_by: str | None = None,
) -> float | None:
    """区间科目在某个财年的值（利润表、现金流量表用）。

    和 `instant_on` 一样支持 `filed_by` —— 道理相同：
    后续年报会重述前两年的对照数，核对具体文件时必须限定披露日。
    """
    best: Observation | None = None
    # 同 instant_on：按披露日筛就必须看未去重的原始序列
    for o in annual_series(facts, tag, unit, taxonomy, dedupe=False):
        if o.end != end:
            continue
        if filed_by is not None and o.filed > filed_by:
            continue
        if best is None or o.filed > best.filed:
            best = o
    return best.value if best is not None else None


def first_filed_by_fiscal_end(facts: dict, unit: str = "USD") -> dict[str, str]:
    """每个财年期末**首次**被申报的日期。

    ## 为什么必须用首次申报日，而不是 `annual_series` 里带的那个

    实测踩到的坑（Oracle）：`annual_series` 按「同一期末保留最晚申报」去重，
    而**每个新财年的 10-K 都会重述前两年的对照数**。结果 FY2024 的数值
    挂在了 FY2026 的申报日上：

        end=2023-05-31  filed=2025-06-18   ← 实际首次申报在 2023-06
        end=2024-05-31  filed=2026-06-22   ← 实际首次申报在 2024-06

    于是按「最晚申报日 ≤ 基准日」筛，**所有历史财年都被排除**。

    判断「这份财报在当时能不能看到」，要看它**第一次公开**是什么时候。
    """
    out: dict[str, str] = {}
    for tag in _REVENUE_TAGS:
        for o in extract_series(facts, tag, unit, duration="annual"):
            if not o.filed:
                continue
            prev = out.get(o.end)
            if prev is None or o.filed < prev:
                out[o.end] = o.filed
    return out


def annual_series_as_of(
    facts: dict, tag: str, as_of: str, unit: str = "USD", taxonomy: str = "us-gaap",
) -> list[Observation]:
    """只保留 `as_of` 之前**已经披露**的年度值。

    做回看或对照分析时必须用这个 —— 否则会把当时还不存在的数据算进去，
    让结果好得不真实。"""
    first_filed = first_filed_by_fiscal_end(facts, unit)
    return [
        o for o in annual_series(facts, tag, unit, taxonomy)
        if first_filed.get(o.end, "9999") <= as_of
    ]


def latest_value(facts: dict, tag: str, unit: str = "USD",
                 taxonomy: str = "us-gaap") -> Observation | None:
    """取最近一条观察值。"""
    series = extract_series(facts, tag, unit, taxonomy)
    return series[-1] if series else None


def value_for_period(facts: dict, tag: str, end: str, unit: str = "USD",
                     taxonomy: str = "us-gaap") -> Observation | None:
    """取指定期末的观察值（同一期末有多条时取最晚申报的）。"""
    matches = [o for o in extract_series(facts, tag, unit, taxonomy) if o.end == end]
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# 从 XBRL 标签推算常用科目
#
# **EDGAR 没有"营业收入"和"EBITDA"这两个现成科目。** 每家公司用的标签不同，
# 而且会随会计准则更新换标签（Apple 的 Revenues 停在 2018 年，之后换成了
# RevenueFromContractWithCustomerExcludingAssessedTax）。
#
# 所以每个推算函数都：
#   - 按优先级尝试多个候选标签
#   - **返回用了哪个标签**，让人能核对
#   - 算不出来返回 None，**绝不猜**
# ---------------------------------------------------------------------------

_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)

# 营业利润的常见标签，按优先级尝试
_OPERATING_INCOME_TAGS = (
    "OperatingIncomeLoss",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)

# 折旧摊销的常见标签
_DA_TAGS = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    "Depreciation",
)

# 股数。⚠️ 单位是 `"shares"`，不是 USD —— 用错单位会静默取到空。
# `EntityCommonStockSharesOutstanding` 在 **dei 命名空间**，是**申报封面**上写的那个数，
# 它的日期是**封面日**、通常比财年期末晚几周。所以取它时必须把"哪个日期的股数"
# 一并说出来（市值 = 价格 × 股数，**两者时点要说得清**）。
_SHARES_TAGS = (
    "EntityCommonStockSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "CommonStockSharesOutstanding",
    "WeightedAverageNumberOfDilutedSharesOutstanding",
)

# 净利润 —— 算 P/E 的分母
_NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")

# 所有者权益 —— 算 P/B 的分母（时点科目，没有 start）
_EQUITY_TAGS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)

# ---------------------------------------------------------------------------
# 净债务（算 EV 用）—— **这组标签的选择直接决定倍数的大小**，所以逐条写清理由
# ---------------------------------------------------------------------------
#
# ## 定义（本项目采用的、会公开写在依据里的口径）
#
#     净债务 = 有息负债 − 现金类
#     有息负债 = 短期债务 + 长期债务 + **融资租赁**负债
#     现金类   = 现金及现金等价物 + 短期投资
#
# ## 逐条的理由
#
# · **含融资租赁**：它是实打实的付息义务 —— 融资租赁 = 借钱买东西。
# · **不含经营租赁**：US GAAP（ASC 842）把经营租赁也上表，但市场做 EV 时
#   通常不把它算进债务（口径不统一，所以这里选了保守的一边，并在依据里明说）。
# · **含短期投资**：可快速变现、性质接近现金（这是常见做法，但**不是唯一做法**）。
# · **衍生品、递延税、应付账款一律不算** —— 它们不是融资性负债。
#
# ⚠️ **长期债务的两套写法只能取一套**：有的公司报 `LongTermDebt`（合计），
#    有的拆成 `LongTermDebtCurrent` + `LongTermDebtNoncurrent`（流动/非流动）。
#    两套都加会**双计**，倍数直接算错 —— 取数时优先用合计，合计没有才用拆分。
#
# ⚠️ **取不到就报缺，不许拿 0 填**：把缺失当成"没有债务"会**系统性低估 EV**，
#    而且从数字上看不出来 —— 这正是本项目最不许出现的那类错。
_DEBT_TOTAL_TAGS = ("LongTermDebt",)
_DEBT_CURRENT_TAGS = (
    "LongTermDebtCurrent",
    "DebtCurrent",
    "ShortTermBorrowings",
    "NotesPayableCurrent",
    "CommercialPaper",
)
_DEBT_NONCURRENT_TAGS = ("LongTermDebtNoncurrent", "LongTermNotesPayable")
_FINANCE_LEASE_TAGS = ("FinanceLeaseLiability", "FinanceLeaseLiabilityNoncurrent",
                       "FinanceLeaseLiabilityCurrent",
                       "CapitalLeaseObligations", "CapitalLeaseObligationsNoncurrent",
                       "CapitalLeaseObligationsCurrent")
_CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "CashAndDueFromBanks",
)
_SHORT_INVEST_TAGS = (
    "ShortTermInvestments",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "MarketableSecuritiesCurrent",
    "OtherShortTermInvestments",
)


@dataclass
class DerivedLine:
    """一个推算出来的科目。带标签来源，方便核对。"""

    name: str
    value: float
    tag: str
    observation: Observation
    note: str


def _first_available(
    facts: dict, tags: tuple[str, ...], end: str, unit: str,
    duration: int | str | None = "annual",
) -> tuple[Observation, str] | None:
    """按优先级找第一个在指定期末有数据的标签。

    duration 必须显式指定 —— 不区分年度/季度会把两种口径混起来。
    """
    for tag in tags:
        series = extract_series(facts, tag, unit, duration=duration)
        matches = [o for o in series if o.end == end]
        if matches:
            return matches[-1], tag
    return None


def derive_revenue(facts: dict, end: str, unit: str = "USD") -> DerivedLine | None:
    """推算营业收入。EDGAR 没有统一的"营业收入"标签。"""
    hit = _first_available(facts, _REVENUE_TAGS, end, unit, "annual")
    if not hit:
        return None
    obs, tag = hit
    return DerivedLine(
        name="营业收入", value=obs.value, tag=tag, observation=obs,
        note=f"来自 XBRL 标签 {tag}（期末 {end}，申报 {obs.filed}）。"
             f"该标签数据不一定是全公司口径，核对后再用。",
    )


def derive_ebitda(facts: dict, end: str, unit: str = "USD") -> DerivedLine | None:
    """从营业利润 + 折旧摊销推算 EBITDA。

    **这不是一个可以直接抓的科目。** 不同公司用不同标签，有的把 D&A 拆进多个科目。

    **缺 D&A 时不要用"行业平均折旧率"去填** —— 那正是估值里最阴险的错误：
    不报错，但结果全错。
    """
    oi_hit = _first_available(facts, _OPERATING_INCOME_TAGS, end, unit, "annual")
    da_hit = _first_available(facts, _DA_TAGS, end, unit, "annual")

    # 缺任一项就返回 None。**不要用"行业平均折旧率"去填** ——
    # 那正是估值里最阴险的错误：不报错，但结果全错。
    if oi_hit is None or da_hit is None:
        return None

    oi, oi_tag = oi_hit
    da, da_tag = da_hit

    return DerivedLine(
        name="EBITDA",
        value=oi.value + da.value,
        tag=f"{oi_tag} + {da_tag}",
        observation=oi,
        note=f"EBITDA = {oi_tag} {oi.value:,.0f} + {da_tag} {da.value:,.0f}"
             f"（期末 {end}，申报 {oi.filed}）。"
             f"推理所得，不是披露科目 —— 核对后再用。",
    )


# ---------------------------------------------------------------------------
# 跨公司查询 —— 可比公司的现成答案
# ---------------------------------------------------------------------------

def frames(tag: str, period: str, taxonomy: str = "us-gaap",
           unit: str = "USD", use_cache: bool = True) -> list[dict]:
    """一个科目、一个期间、**所有申报人**的值。

    period 格式：
        CY2024          年度
        CY2024Q4        季度
        CY2024Q4I       时点（资产负债表科目用这个）

    实测一次调用返回 6,000+ 家公司的数据 —— 这就是可比公司筛选的原料。
    """
    url = f"{BASE}/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
    data = _cached_get(url, purpose=f"frames {tag} {period}", use_cache=use_cache)
    return data.get("data", [])


def ticker_to_cik(ticker: str, use_cache: bool = True) -> str | None:
    """股票代码 → CIK。"""
    url = f"{WWW}/files/company_tickers.json"
    data = _cached_get(url, purpose="ticker map", use_cache=use_cache)
    t = ticker.upper()
    for item in data.values():
        if str(item.get("ticker", "")).upper() == t:
            return _normalize_cik(item["cik_str"])
    return None


def peers_by_tag_value(
    tag: str,
    period: str,
    low: float | None = None,
    high: float | None = None,
    limit: int = 50,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
) -> list[dict]:
    """按科目值筛出一组公司 —— 例如"总资产在 1 亿到 10 亿美元之间"。

    **注意：这里筛的是规模，不是行业。** 行业筛选需要 SIC 代码，
    要另外调 submissions 接口。规模筛选已经能把可比集合砍到可用的量级。
    """
    rows = frames(tag, period, taxonomy, unit)
    out = []
    for r in rows:
        v = r.get("val")
        if v is None:
            continue
        if low is not None and v < low:
            continue
        if high is not None and v > high:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 市值类指标（P/E、P/B、市值/收入）要用到的三个量：股数 / 净利润 / 所有者权益
# ---------------------------------------------------------------------------
#
# ★ 为什么单列一节：`derive_ebitda` 走的是**经营口径**，而 P/E、P/B 要的是**股东口径** ——
#   分母不同、命名空间不同（股数在 `dei`）、**单位也不同（"shares"，不是 USD）**。
#   单位写错会**静默取到空**，然后被当成"这家公司没有数据"。

def _latest_instant(
    facts: dict,
    tags: tuple[str, ...],
    unit: str,
    taxonomies: tuple[str, ...] = ("us-gaap",),
    on: str | None = None,
) -> tuple[Observation, str, str] | None:
    """取**不晚于** `on` 的最近一个时点值（`on` 为空则取最后一个）。

    「不晚于」是硬要求：拿一个**晚于**基准日的股数去乘基准日的价格，
    等于把两个时点的东西拼在一起 —— 和价格那条纪律（不许回退到最新价）同一个道理。
    """
    best: tuple[Observation, str, str] | None = None
    for taxo in taxonomies:
        for tag in tags:
            for o in extract_series(facts, tag, unit, taxonomy=taxo, duration=None):
                if on and o.end > on:
                    continue
                if best is None or o.end > best[0].end:
                    best = (o, tag, taxo)
    return best


def shares_outstanding(facts: dict, on: str | None = None) -> DerivedLine | None:
    """取股数。返回里**必须带日期** —— 它是申报封面日，不是财年期末。"""
    hit = _latest_instant(facts, _SHARES_TAGS, "shares", ("dei", "us-gaap"), on)
    if hit is None:
        return None
    obs, tag, taxo = hit
    return DerivedLine(
        name="股数", value=obs.value, tag=tag, observation=obs,
        note=f"来自 {taxo}:{tag} —— **这个数的日期是 {obs.end}**"
             f"（申报封面日，通常比财年期末晚几周）。市值 = 价格 × 股数，"
             f"价格还是要按基准日取，两个时点都说得清才不会拼错。",
    )


def net_income_of(facts: dict, end: str, unit: str = "USD") -> DerivedLine | None:
    """取净利润。**股东口径**，和 `derive_ebitda` 的经营口径不是一回事。"""
    hit = _first_available(facts, _NET_INCOME_TAGS, end, unit, "annual")
    if hit is None:
        return None
    obs, tag = hit
    return DerivedLine(
        name="净利润", value=obs.value, tag=tag, observation=obs,
        note=f"来自 XBRL 标签 {tag}（期末 {end}，申报 {obs.filed}）。",
    )


def equity_of(facts: dict, end: str, unit: str = "USD") -> DerivedLine | None:
    """取所有者权益（**时点科目** —— 没有期间，所以 duration 传 None）。"""
    hit = _first_available(facts, _EQUITY_TAGS, end, unit, None)
    if hit is None:
        return None
    obs, tag = hit
    return DerivedLine(
        name="所有者权益", value=obs.value, tag=tag, observation=obs,
        note=f"来自 XBRL 标签 {tag}（时点 {end}，申报 {obs.filed}）。",
    )


def derive_ebit(facts: dict, end: str, unit: str = "USD") -> DerivedLine | None:
    """取 EBIT —— **用营业利润代替**（EDGAR 里没有统一的 EBIT 标签）。

    这不是猜：EBIT（息税前利润）与营业利润只差**利息与营业外收支**那一层。
    但两者**不总是相等**，所以 note 里明说"用营业利润代替"，
    免得有人把这个数拿去和别处口径的 EBIT 直接比。
    """
    hit = _first_available(facts, _OPERATING_INCOME_TAGS, end, unit, "annual")
    if hit is None:
        return None
    obs, tag = hit
    return DerivedLine(
        name="EBIT（用营业利润代替）", value=obs.value, tag=tag, observation=obs,
        note=f"来自 XBRL 标签 {tag}（期末 {end}，申报 {obs.filed}）——"
             f"**EDGAR 没有统一的 EBIT 标签，这里用营业利润代替**，"
             f"与别处口径的 EBIT 可能差利息与营业外收支。",
    )


def _instant_at(facts: dict, tags: tuple[str, ...], end: str, unit: str):
    """在**指定时点**取第一个有值的标签。时点科目用 duration=None。"""
    for tag in tags:
        hits = [o for o in extract_series(facts, tag, unit, duration=None)
                if o.end == end]
        if hits:
            return hits[-1], tag
    return None, ""


def net_debt_of(facts: dict, end: str, unit: str = "USD") -> DerivedLine | None:
    """净债务 = 有息负债 − 现金类（口径见 `_DEBT_*` 那一节的逐条理由）。

    ## 三条不能破的规矩
    1. **长期债务的两套写法只取一套** —— `LongTermDebt`（合计）优先，
       没有合计才用 `LongTermDebtCurrent` + `LongTermDebtNoncurrent`。
       两套都加会**双计**，倍数直接算错。
    2. **取不到就返回 None，绝不拿 0 填** —— 把缺失当成「没有债务」
       会**系统性低估 EV**，而且从数字上看不出来。
    3. **返回值里带组成** —— 净债务是"口径决定结果"的东西，
       note 里把每一项的来源与金额都写出来，让人能逐项核对，
       而不是只看到一个合计数。
    """
    parts: list[str] = []
    total = 0.0

    # ① 长期债务：合计优先，拆分次之（**不许两套都加**）
    lt_hit, lt_tag = _instant_at(facts, _DEBT_TOTAL_TAGS, end, unit)
    if lt_hit is not None:
        total += lt_hit.value
        parts.append(f"长期债务 {lt_hit.value:,.0f}（{lt_tag}）")
    else:
        for tags, label in ((_DEBT_CURRENT_TAGS, "一年内到期/短期"), 
                            (_DEBT_NONCURRENT_TAGS, "非流动")):
            hit, tag = _instant_at(facts, tags, end, unit)
            if hit is not None:
                total += hit.value
                parts.append(f"{label} {hit.value:,.0f}（{tag}）")

    # ② 融资租赁：付息义务，算进来
    lease_hit, lease_tag = _instant_at(facts, _FINANCE_LEASE_TAGS, end, unit)
    if lease_hit is not None:
        total += lease_hit.value
        parts.append(f"融资租赁 {lease_hit.value:,.0f}（{lease_tag}）")

    # ③ 现金类：减掉
    cash_hit, cash_tag = _instant_at(facts, _CASH_TAGS, end, unit)
    if cash_hit is None:
        return None                       # 没有现金 → 净债务算不出来，报缺
    total -= cash_hit.value
    parts.append(f"现金 {cash_hit.value:,.0f}（{cash_tag}）")
    si_hit, si_tag = _instant_at(facts, _SHORT_INVEST_TAGS, end, unit)
    if si_hit is not None:
        total -= si_hit.value
        parts.append(f"短期投资 {si_hit.value:,.0f}（{si_tag}）")

    # 一个**债务类**科目都没取到 → 不做推断（可能是真无债，也可能是没报，分不开）
    debt_parts = [p for p in parts if not p.startswith(("现金", "短期投资"))]
    if not debt_parts:
        return None

    return DerivedLine(
        name="净债务", value=total, tag="+".join(p.split("（")[-1].rstrip("）")
                                                for p in parts),
        observation=cash_hit,
        note=f"口径：有息负债（含融资租赁）− 现金类。组成：{'；'.join(parts)}"
             f"（时点 {end}）。**不含经营租赁、不含应付账款与递延税。**",
    )
