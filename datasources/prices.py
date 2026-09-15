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

## 市场前缀（已实测验证）

| 前缀 | 市场 | 例子 |
|---|---|---|
| `1.` | 沪市 | 1.600519 |
| `0.` | 深市 / 创业板 | 0.000001 / 0.300750 |
| `105.` | 美股 | 105.AAPL |
| `116.` | 港股 | 116.00700 |
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

import net

BASE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

MARKET_PREFIX = {
    "sh": "1",       # 沪市主板 / 科创板
    "sz": "0",       # 深市主板 / 创业板
    "us": "105",     # 美股
    "hk": "116",     # 港股
}

# 东财不需要 User-Agent 里的邮箱，但带上更像正常浏览器
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

_MIN_INTERVAL = 0.25
_last_call = [0.0]

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "prices"
CACHE_TTL = 6 * 3600


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


def resolve_secid(market: str, code: str) -> str:
    """市场 + 代码 → 东财 secid。

    market 用 'sh' / 'sz' / 'us' / 'hk'。用代码前缀猜市场是常见的错误源，
    所以要求显式指定。
    """
    key = market.strip().lower()
    if key not in MARKET_PREFIX:
        raise PriceError(
            f"不认识的市场：{market!r}（用 {', '.join(MARKET_PREFIX)}）"
        )
    c = str(code).strip().upper()
    if not c:
        raise PriceError("代码不能为空")
    return f"{MARKET_PREFIX[key]}.{c}"


def _throttle() -> None:
    delta = time.time() - _last_call[0]
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _last_call[0] = time.time()


def _fetch(secid: str, beg: str, end: str, adjust: int) -> list[str]:
    """取原始 kline 行（每行是 '日期,收盘价'）。"""
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

    _throttle()
    raw = net.guarded_get(
        url,
        net.PublicQuery({"secid": secid, "beg": beg, "end": end}, purpose="行情日线"),
        timeout=30,
        headers={"User-Agent": USER_AGENT},
    )

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PriceError(f"东财返回了无法解析的内容（{e}）。端点可能改了。") from e

    data = payload.get("data")
    if not data:
        return []
    rows = data.get("klines") or []
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


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
    secid: str,
    start: str,
    end: str,
    adjust: int = 0,
) -> list[PriceBar]:
    """一段区间的日收盘价。adjust=0 是不复权（算市值用这个）。"""
    return _parse_rows(_fetch(secid, start, end, adjust), secid, adjust)


def close_on(secid: str, as_of: str, adjust: int = 0, lookback_days: int = 15) -> PriceBar | None:
    """取**不晚于** as_of 的最近一个交易日收盘价。

    这是估值应该用的接口：基准日是 2025-12-31，就用 2025-12-31 或之前
    最近一个交易日的价。**不会用今天的价去配去年的报表。**

    lookback 覆盖长假（春节、国庆）。找不到就返回 None ——
    不要回退到"最新的价"，那是另一种口径。
    """
    from datetime import date, timedelta

    try:
        end_d = date.fromisoformat(as_of)
    except ValueError as e:
        raise PriceError(f"基准日格式必须是 YYYY-MM-DD，收到 {as_of!r}") from e

    start_d = end_d - timedelta(days=lookback_days)
    bars = _parse_rows(
        _fetch(secid, start_d.isoformat(), end_d.isoformat(), adjust),
        secid, adjust,
    )
    eligible = [b for b in bars if b.date <= as_of]
    return eligible[-1] if eligible else None


def latest_close(secid: str, adjust: int = 0, days_back: int = 20) -> PriceBar | None:
    """最近一个交易日的收盘价。

    **这个函数是给"看现在的行情"用的，不是给估值用的。**
    估值请用 close_on(secid, 基准日)。
    """
    from datetime import date, timedelta

    end_d = date.today()
    start_d = end_d - timedelta(days=days_back)
    bars = daily_closes(secid, start_d.isoformat(), end_d.isoformat(), adjust)
    return bars[-1] if bars else None
