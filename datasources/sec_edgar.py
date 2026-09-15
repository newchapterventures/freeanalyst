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
    duration: int | str = "annual",
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
