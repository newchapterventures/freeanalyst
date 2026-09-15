"""价格数据源 —— 东方财富历史日线。

## 为什么用"指定日期的收盘价"而不是实时报价

实测发现东财的实时报价端点（`push2`）**间歇性返回空响应，连打 6 次全失败**，
而历史日线端点（`push2his`）稳定可用。

**但即使实时报价能用，也不该用它做估值。** 实时价是个陷阱：

> 你会在一个周二下午，把今天的股价配上去年 12 月 31 日的资产负债表，
> 然后把这个数写进估值报告。

估值要的是「**基准日**的市值」，不是「现在的市值」。
所以这个模块的接口是按日期取价，不提供 `current_price()`。

## 复权：估值要用不复权价

| fqt | 含义 | 用途 |
|---|---|---|
| 0 | **不复权**（真实成交价） | ✅ 算市值 —— 配对应日期的股本才是对的 |
| 1 | 前复权 | 算收益率、技术指标 |
| 2 | 后复权 | 长期收益率 |

**市值 = 价格 × 股本，两者必须是同一时点、同一口径。**
用前复权价配当期股本，除权那天起就会算错。

## 市场前缀：A 股港股是确定的，美股必须探测

| 市场 | 前缀 | 依据 |
|---|---|---|
| `sh` 沪市 | `1.` | 实测 |
| `sz` 深市 / 创业板 | `0.` | 实测 |
| `hk` 港股 | `116.` | 实测 |
| `us` 美股 | **不确定** | ↓ 见下 |

美股前缀看起来像"105=纳斯达克、106=纽交所"，但**这条规则不成立**：

```
AAPL MSFT GOOGL NVDA AMZN META TSLA   → 105   （都是纳斯达克）
ORCL JPM KO DIS BA CAT V XOM PFE T    → 106   （都是纽交所）
WMT                                    → 105   ← 纽交所，但走 105
SPY                                    → 107   （ETF）
```

沃尔玛是纽交所上市的，却挂在 105 下。所以**不能靠映射表猜**，
只能按 105 → 106 → 107 顺序探测，命中的记下来。

**探测结果会缓存**，所以每个 ticker 只多花一次请求。

## 公开接口按 (市场, 代码) 而不是 secid

`close_on("us", "ORCL", "2025-12-31")` —— 调用方不需要知道前缀是什么。
前缀是这个数据源的实现细节，不该泄漏出去。
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import net

BASE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# 确定的市场前缀
MARKET_PREFIX = {
    "sh": "1",       # 沪市主板 / 科创板
    "sz": "0",       # 深市主板 / 创业板
    "hk": "116",     # 港股
}

# 美股要探测的顺序。105=纳斯达克、106=纽交所、107=ETF，
# 但 WMT 走 105 说明规则不严，只能按顺序试。
US_PREFIXES = ("105", "106", "107")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

_MIN_INTERVAL = 0.25
_last_call = [0.0]

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "prices"
CACHE_TTL = 6 * 3600
_PREFIX_CACHE = CACHE_DIR / "us_prefix.json"


class PriceError(RuntimeError):
    pass


@dataclass
class PriceBar:
    """一个交易日的收盘价。"""

    date: str
    close: float
    secid: str
    adjust: int

    def __str__(self) -> str:
        return f"{self.date}  {self.close:,.2f}"


def _throttle() -> None:
    delta = time.time() - _last_call[0]
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _last_call[0] = time.time()


def _load_prefix_cache() -> dict[str, str]:
    try:
        return json.loads(_PREFIX_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_prefix_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _PREFIX_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 缓存写不了不影响功能


def resolve_secid(market: str, code: str) -> str:
    """市场 + 代码 → 东财 secid。

    A 股和港股是确定的前缀。**美股只返回首选（105）**，真正的探测在
    `daily_closes` / `close_on` 内部做 —— 那里才知道哪个前缀有数据。

    想单独拿到可用的 secid，用 `probe_secid()`。
    """
    key = market.strip().lower()
    c = str(code).strip().upper()
    if not c:
        raise PriceError("代码不能为空")
    if key in MARKET_PREFIX:
        return f"{MARKET_PREFIX[key]}.{c}"
    if key == "us":
        cached = _load_prefix_cache().get(c)
        return f"{cached or US_PREFIXES[0]}.{c}"
    raise PriceError(
        f"不认识的市场：{market!r}（用 sh / sz / us / hk）"
    )


def _candidate_secids(market: str, code: str) -> list[str]:
    key = market.strip().lower()
    c = str(code).strip().upper()
    if key == "us":
        cache = _load_prefix_cache()
        if c in cache:
            # 命中缓存的前缀放最前，其余按顺序兜底
            rest = [p for p in US_PREFIXES if p != cache[c]]
            return [f"{cache[c]}.{c}"] + [f"{p}.{c}" for p in rest]
        return [f"{p}.{c}" for p in US_PREFIXES]
    return [resolve_secid(market, code)]


def _fetch_rows(secid: str, beg: str, end: str, adjust: int) -> list[str] | None:
    """取原始 kline 行。**没有数据返回 None，区别于空列表。**

    东财用 `{"rc":100, "data":null}` 表示"这个 secid 不存在"，
    用空 klines 表示"存在但没有该区间的数据"。两种情况要分开处理。

    ## 连接会被掐断，而且掐断时不给任何响应

    实测：查一个不存在的代码（`1.999999`），东财**直接关闭连接**，
    客户端收到 `RemoteDisconnected`，和 curl 的 exit 52（Empty reply）是同一回事。

    这带来一个**危险的歧义**：拿不到数据可能是
      (a) 代码不存在  (b) 网络断了  (c) 被限流

    **所以这里不能静默返回"无数据"。** 那会让用户以为"这家公司没有价格数据"，
    而实际是网络问题 —— 结果是一个悄悄缺了几家公司的可比集合。

    处理方式：重试三次；仍然断开就抛错，并把三种可能讲清楚。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = urllib.parse.quote(f"{secid}_{beg}_{end}_{adjust}", safe="")
    path = CACHE_DIR / f"{key}.json"

    if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    params = {
        "secid": secid,
        "klt": "101",          # 日线
        "fqt": str(adjust),
        "beg": beg.replace("-", ""),
        "end": end.replace("-", ""),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56",   # 日期,开,收,高,低,量
    }
    url = f"{BASE}?{urllib.parse.urlencode(params)}"

    raw: bytes | None = None
    for attempt in range(3):
        _throttle()
        try:
            raw = net.guarded_get(
                url,
                net.PublicQuery({"secid": secid, "beg": beg, "end": end},
                                purpose="行情日线"),
                timeout=30,
                headers={"User-Agent": USER_AGENT},
            )
            break
        except (OSError, http.client.HTTPException) as e:
            if attempt == 2:
                raise PriceError(
                    f"查询 {secid} 时东财断开了连接，重试 3 次都没成功。\n"
                    f"三种可能，请核实是哪一种：\n"
                    f"  1. 这个代码在东财不存在（实测不存在的代码会被直接掐连接）\n"
                    f"  2. 网络问题\n"
                    f"  3. 请求被限流（当前限速 {1 / _MIN_INTERVAL:.0f} 次/秒）\n\n"
                    f"**不把它当成「没有价格数据」处理** —— 那会悄悄漏掉可比公司。"
                ) from e
            time.sleep(0.5 * (attempt + 1))
    if raw is None:
        return None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PriceError(f"东财返回了无法解析的内容（{e}）。端点可能改了。") from e

    rows = (payload.get("data") or {}).get("klines")
    if not rows:
        return None
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def _fetch(
    market: str, code: str, beg: str, end: str, adjust: int
) -> tuple[list[str], str] | tuple[None, None]:
    """按候选前缀依次探测，返回 (原始行, 命中的 secid)。全试完没数据返回 (None, None)。"""
    c = str(code).strip().upper()
    for secid in _candidate_secids(market, code):
        rows = _fetch_rows(secid, beg, end, adjust)
        if rows:
            if market.strip().lower() == "us":
                cache = _load_prefix_cache()
                if cache.get(c) != secid.split(".", 1)[0]:
                    cache[c] = secid.split(".", 1)[0]
                    _save_prefix_cache(cache)
            return rows, secid
    return None, None


def _parse_rows(rows: list[str], secid: str, adjust: int) -> list[PriceBar]:
    out: list[PriceBar] = []
    for line in rows:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            out.append(PriceBar(date=parts[0], close=float(parts[2]),
                                secid=secid, adjust=adjust))
        except ValueError:
            continue
    out.sort(key=lambda b: b.date)
    return out


def daily_closes(
    market: str, code: str, start: str, end: str, adjust: int = 0
) -> list[PriceBar]:
    """一段区间的日收盘价。adjust=0 是不复权（算市值用这个）。"""
    rows, secid = _fetch(market, code, start, end, adjust)
    if not rows or secid is None:
        return []
    return _parse_rows(rows, secid, adjust)


def close_on(
    market: str, code: str, as_of: str, adjust: int = 0, lookback_days: int = 15
) -> PriceBar | None:
    """取**不晚于** as_of 的最近一个交易日收盘价。

    这是估值应该用的接口：基准日是 2025-12-31，就用 2025-12-31 或之前
    最近一个交易日的价。**不会用今天的价去配去年的报表。**

    lookback 覆盖长假（春节、国庆）。找不到就返回 None ——
    **不要回退到"最新的价"，那是另一种口径。**
    """
    from datetime import date, timedelta

    try:
        end_d = date.fromisoformat(as_of)
    except ValueError as e:
        raise PriceError(f"基准日格式必须是 YYYY-MM-DD，收到 {as_of!r}") from e

    start_d = end_d - timedelta(days=lookback_days)
    rows, secid = _fetch(market, code, start_d.isoformat(), end_d.isoformat(), adjust)
    if not rows or secid is None:
        return None
    eligible = [b for b in _parse_rows(rows, secid, adjust) if b.date <= as_of]
    return eligible[-1] if eligible else None


def probe_secid(market: str, code: str) -> str | None:
    """联网探测出这个代码真正可用的 secid。找不到返回 None。"""
    from datetime import date, timedelta

    end_d = date.today()
    start_d = end_d - timedelta(days=10)
    rows, secid = _fetch(market, code, start_d.isoformat(), end_d.isoformat(), 0)
    return secid if rows else None


def latest_close(market: str, code: str, adjust: int = 0, days_back: int = 20) -> PriceBar | None:
    """最近一个交易日的收盘价。

    **这个函数是给"看现在的行情"用的，不是给估值用的。**
    估值请用 close_on(market, code, 基准日)。
    """
    from datetime import date, timedelta

    end_d = date.today()
    start_d = end_d - timedelta(days=days_back)
    bars = daily_closes(market, code, start_d.isoformat(), end_d.isoformat(), adjust)
    return bars[-1] if bars else None
