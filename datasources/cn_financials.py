"""A 股财务数据 —— 东方财富 `datacenter` 的结构化报表接口。

## 这份模块解决什么

取一家 A 股公司在**某个报告期**的四个概念（外加一个便于核对的）：

    营业收入 / 净利润（归母） / 营业利润 / 所有者权益（归母） / 所有者权益合计

每个值都带回**来源字段名**与**报告期** —— 因为"取到的是哪个口径"比"取到了数"更重要。

## 字段名与模板名不是猜的，是量过的

出处：`docs/数据源-中港财务.md`（2026-09-29 实测）。本模块的端点、模板名、
候选字段名**逐字来自那里**，不在此另立一套。

    端点：https://datacenter.eastmoney.com/securities/api/data/v1/get
    参数：reportName / columns=ALL / filter / pageNumber / pageSize
          / sortTypes=-1 / sortColumns=REPORT_DATE / source=HSF10 / client=PC

## 四套模板：金融业与一般工商业的科目不是一套

| 行业 | 利润表 | 资产负债表 |
|---|---|---|
| 通用 | `GINCOME` | `GBALANCE` |
| 银行 | `BINCOME` | `BBALANCE` |
| 保险 | `IINCOME` | `IBALANCE` |
| 证券 | `SINCOME` | `SBALANCE` |

（本模块只用利润表和资产负债表两张 —— 要的五个概念都在这两张里。）

## 行业模板怎么判断：**不能从利润表看**

这一点是本模块实测补上的，文档里没写，所以在这里说清楚：

**通用利润表 `GINCOME` 对银行和券商一样有数据**。实测（2026-06-30 一期）：

    601398 工商银行   GINCOME 有数据   BINCOME 也有数据
    600030 中信证券   GINCOME 有数据   SINCOME 也有数据

所以"先试通用、通则算通用"会把银行和券商都判成通用，**专用模板永远轮不到**。
判据改用**资产负债表**：四套里只有一套有数据，且互不重叠 —— 实测：

    600519  GBALANCE 有 · BBALANCE 空
    601398  GBALANCE 空  · BBALANCE 有
    601601  GBALANCE 空  · IBALANCE 有
    600030  GBALANCE 空  · SBALANCE 有

于是 `detect_industry()` 按 通用 → 银行 → 保险 → 证券 的顺序试**资产负债表**，
第一个非空的即为该公司的行业，随后利润表也用同一行业的专用模板
（保证一张结果里的口径是同族的）。调用方也可以用 `industry=` 跳过探测。

## 报告期口径：利润表是**年内累计**，不是单季

实测 600519 的营业总收入（元）：

    2024-12-31   174,144,069,958.25    全年
    2025-06-30    91,093,762,553.97    上半年累计（约等于全年的一半）
    2025-12-31   172,054,171,890.91    全年
    2026-06-30    92,278,072,083.21    上半年累计

再对上更早的 2019-03-31（22,480,525,254.69，Q1）与 2019-06-30
（41,172,681,309.94，H1）——**累计**，不是单季。做同比/年化时口径要先对齐。

## 出口：走项目自己的闸门，不动 `net.py` 的白名单

联网一律走 `net.guarded_get`，姿势照抄 `datasources/sec_edgar.py`：
构造 `net.PublicQuery`（参数出去前会被长度/字符集校验），再交给 `guarded_get`。

`datacenter.eastmoney.com` **不在** `net.py` 的 `DEFAULT_ALLOWED_HOSTS` 里，
本模块用 `PublicQuery(extra_hosts=EXTRA_HOSTS)` **就地声明**这一个主机
（`net.allowed_hosts()` 支持这个口子），**没有改 `net.py`**。
要不要把它并进全局白名单，由维护者决定 —— 本模块只声明自己要用的那个。

## 失败 ≠ 没有数据

东财今天被频繁探测时会**间歇性拒连**（连 curl 都会），表现为连接被直接掐断
（`RemoteDisconnected`），也可能是一个 5xx。这种时候**不能返回 None** ——
那会让调用方以为"这家公司没有财务数据"，从而悄悄从可比集合里漏掉一家。

所以：

| 情况 | 行为 |
|---|---|
| 网络断连 / 5xx | 重试（指数退避 + 抖动），仍失败 → 抛 `CnFinancialsError`，写明三种可能 |
| HTTP 200 但表里没这一期 | 正常返回，五个概念全部 `None` + `gaps` 里写明原因 |
| 返回的不是 JSON | 抛 `CnFinancialsError`（端点可能改了），**不当成"没有数据"** |

## 缺就缺，不拿 0 填、不估算

字段不存在、值为 `None`、值不是数字 —— 三种都按**缺**处理（`None` + 说明）。
**`0` 是合法值**，不会被当成缺。折旧摊销取不到（文档已实测）所以这里不碰 EBITDA。

## 没验的（诚实边界）

- **专用模板覆盖的历史期间范围**：通用表实测能追到 2019 年，专用表只对过 2026 中报。
- **代码前缀**：只写实测通的首位映射（6→SH、0→SZ、92→BJ）。其余（3xxxxx 创业板、
  688xxx 科创板、900xxx 沪 B、830799.BJ 这类北交所老代码）本模块不猜 ——
  "首位定市场"本身不成立（9 既可能是沪 B、也可能是北交所 920xxx），
  想用请显式传完整 `SECUCODE`，如 `"300750.SZ"`。
- **北交所老代码**：`830799.BJ` / `832566.BJ` 实测**返回空**，原因未查明；
  `920819.BJ` 实测有数据。
"""

from __future__ import annotations

import http.client
import json
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

import net

BASE = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
HOST = "datacenter.eastmoney.com"

#: 本模块要用的主机 —— 通过 `PublicQuery.extra_hosts` 就地声明。
#: **不是改 `net.py` 的白名单**（那里仍然只有原本那几个主机）。
EXTRA_HOSTS: frozenset[str] = frozenset({HOST})

#: 这个源覆盖的市场与币种 —— 由源自己声明，调用方不许写死（同 `sec_edgar.py`）。
MARKET = "cn"
CURRENCY = "CNY"

#: 这个源**没有行情**（价格/股数/市值）。财务和价格是两个源，
#: 价格见 `datasources/prices.py`（东财日线，另外那个主机）。
HAS_PRICES = False

#: 模板：行业 → (利润表后缀, 资产负债表后缀)。后缀逐字来自
#: `docs/数据源-中港财务.md`，实际端点参数是 `RPT_F10_FINANCE_<后缀>`。
TEMPLATES: dict[str, tuple[str, str]] = {
    "通用": ("GINCOME", "GBALANCE"),
    "银行": ("BINCOME", "BBALANCE"),
    "保险": ("IINCOME", "IBALANCE"),
    "证券": ("SINCOME", "SBALANCE"),
}

#: 探测顺序。**资产负债表**这几套互不重叠（实测），所以第一个非空的就是答案。
TEMPLATE_ORDER: tuple[str, ...] = ("通用", "银行", "保险", "证券")

REPORT_PREFIX = "RPT_F10_FINANCE_"

INCOME = "income"
BALANCE = "balance"

#: 代码首位 → 交易所后缀。**只写实测过的**：
#:   6xxxxx → .SH（600519 / 601398 / 600030 实测通）
#:   0xxxxx → .SZ（000001 实测通）
#:   92xxxx → .BJ（920819 实测通）
#:
#: 其余不写死，理由不是懒：**"首位定市场"这条规则本身不成立** ——
#: 9 打头的既可能是沪市 B 股（900xxx）、也可能是北交所（920xxx，实测通）。
#: 猜错了会取到另一个市场的数据，而那种错**不会报错**、只会给出一组看着正常的数字。
#: 所以未实测量的代码请显式传 SECUCODE（`"300750.SZ"`、`"688981.SH"`）。
MARKET_SUFFIX: dict[str, str] = {
    "6": "SH",
    "0": "SZ",
    "92": "BJ",      # 两字符前缀，必须先于单字符匹配
}

_CODE_RE = re.compile(r"^\d{6}$")
_SUFFIXES = ("SH", "SZ", "BJ")

#: 限速。实测口径与 `prices.py` 同一套理由：东财在 4 次/秒时会偶发掐连接，
#: 降到 2.5 次/秒后明显变稳。取一家公司最多 4 个请求（探测 1 + 利润表 1 +
#: 资产负债表 1，命中缓存时更少），合计约 1 秒。
_MIN_INTERVAL = 0.4

#: 失败后的等待（秒）—— 指数退避 + 抖动。抖动是必要的：多个请求同时被限流时，
#: 固定间隔会让它们**一起**回来再一起撞墙。
_RETRY_WAITS = (0.6, 1.5, 3.2)
_last_call = [0.0]

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

#: 单条结果缓存一天。财报是**季频**数据，一天内不会变；
#: 但**缓存只写"非空"的响应** —— 见 `_fetch_rows()`。
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "cn_financials"
CACHE_TTL = 24 * 3600


class CnFinancialsError(RuntimeError):
    """取数失败。**带可操作说明，不是一句「失败」。**"""


@dataclass(frozen=True)
class Concept:
    """一个要取的概念 ↔ 它在东财表里的候选字段名。"""

    key: str                      # 结果里的键，也是给人看的中文概念名
    candidates: tuple[str, ...]   # 字段名，**按优先级**
    table: str                    # INCOME / BALANCE


#: 候选字段名逐字来自 `docs/数据源-中港财务.md` 的实测表。
#:
#: `营业收入` 有**两个口径**，都实测有值（600519 2024 年：
#: `TOTAL_OPERATE_INCOME` 174,144,069,958.25 vs `OPERATE_INCOME` 170,899,152,276.34，
#: 差 3,244,917,681.91 —— 营业总收入和营业收入不是一回事）。
#: 这里优先取营业总收入，并把**实际用到哪个字段**写进结果，让口径差异看得见；
#: 券商/保险的专用表里没有 `TOTAL_OPERATE_INCOME`（实测），会自动回落到 `OPERATE_INCOME`。
CONCEPTS: tuple[Concept, ...] = (
    Concept("营业收入", ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"), INCOME),
    Concept("净利润(归母)", ("PARENT_NETPROFIT",), INCOME),
    Concept("营业利润", ("OPERATE_PROFIT",), INCOME),
    Concept("所有者权益(归母)", ("TOTAL_PARENT_EQUITY",), BALANCE),
    Concept("所有者权益合计", ("TOTAL_EQUITY",), BALANCE),
)

#: 结果里必须有的四个概念（其余是附带信息）
REQUIRED: tuple[str, ...] = (
    "营业收入", "净利润(归母)", "营业利润", "所有者权益(归母)",
)

#: 报告期口径说明 —— 每次都随结果一起给出去，免得调用方拿累计数当单季数。
PERIOD_NOTE = (
    "利润表按**年内累计**口径列示（中报 = 年初至报告期末累计，不是单季）；"
    "资产负债表是报告期末的时点值。金额单位：元。"
)


# ---------------------------------------------------------------------------
# 代码 → SECUCODE
# ---------------------------------------------------------------------------

def secucode(code: str) -> str:
    """A 股代码 → 东财的 `SECUCODE`（如 `600519.SH`）。

    接受两种写法：`"600519"`（自动补市场后缀，**只补实测过的前缀**）
    或 `"600519.SH"`（原样采用，不做推断）。

    只做**格式**校验，不做"这只票存不存在"的判断 —— 那要看接口答不答，
    而接口答不出来可能是网络问题（见模块文档「失败 ≠ 没有数据」）。
    """
    raw = str(code).strip().upper()
    if not raw:
        raise CnFinancialsError("代码不能为空")

    if "." in raw:
        left, _, right = raw.partition(".")
        if not _CODE_RE.match(left):
            raise CnFinancialsError(f"代码必须是 6 位数字，收到 {code!r}")
        if right not in _SUFFIXES:
            raise CnFinancialsError(
                f"市场后缀 {right!r} 不认识 —— 用 {' / '.join(_SUFFIXES)} 之一"
            )
        return raw

    if not _CODE_RE.match(raw):
        raise CnFinancialsError(
            f"代码必须是 6 位数字（或 6 位数字 + 市场后缀），收到 {code!r}"
        )

    suffix = MARKET_SUFFIX.get(raw[:2]) or MARKET_SUFFIX.get(raw[:1])
    if suffix is None:
        raise CnFinancialsError(
            f"{raw} 的首位没有实测过市场归属 —— 本模块不猜（猜错会取到另一个市场的数据，"
            f"而且不会报错）。请显式传 SECUCODE，例如：\n"
            f"    fetch(\"{raw}.SZ\")   # 深市（创业板 300xxx）\n"
            f"    fetch(\"{raw}.SH\")   # 沪市（科创板 688xxx）\n"
            f"已实测：6→SH、0→SZ、92→BJ。"
        )
    return f"{raw}.{suffix}"


def normalize_report_date(report_date: str) -> str:
    """报告期必须是 `YYYY-MM-DD` —— 东财的 `filter` 里就是这个格式。

    只接受严格格式：`2026/06/30`、`20260630`、`2026-6-3` 一律拒绝，
    因为**静默接受会让 filter 匹配不上、于是返回空**，
    而那会被读成"这家没有这一期的数据"。

    `2026-13-01` 这种"格式对、日子不对"的也拒绝 —— 同样会静默返回空。
    """
    text = str(report_date).strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        raise CnFinancialsError(
            f"报告期格式必须是 YYYY-MM-DD，收到 {report_date!r}"
            f"（东财 filter 用的就是这个格式，写错会静默返回空）"
        )
    try:
        _date.fromisoformat(text)
    except ValueError as e:
        raise CnFinancialsError(
            f"报告期 {report_date!r} 不是一个真实日期（格式对、日子不对）—— "
            f"这种写法同样会被接口静默当成「没有数据」。"
        ) from e
    return text


def _split_secucode(secu: str) -> str:
    """`600519.SH` → `600519`（只用于结果里的 `code` 字段）。"""
    return secu.split(".", 1)[0]


# ---------------------------------------------------------------------------
# 取数：唯一联网的地方
# ---------------------------------------------------------------------------

def _throttle() -> None:
    delta = time.time() - _last_call[0]
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _last_call[0] = time.time()


def _rate_text() -> str:
    """限速的显示形式。**测试会把间隔设成 0**，别在这个字符串上除零。"""
    return f"{1 / _MIN_INTERVAL:.1f}" if _MIN_INTERVAL > 0 else "不限"


def _query(params: dict, purpose: str) -> net.PublicQuery:
    """构造出境查询。

    单独抽出来是为了让"我们确实走了闸门"这件事**可被测试钉住**：
    参数会先过 `PublicQuery` 的长度/字符集校验，主机在 `extra_hosts` 里显式声明。
    """
    return net.PublicQuery(params, purpose=purpose, extra_hosts=EXTRA_HOSTS)


def _filter_for(secu: str, report_date: str | None) -> str:
    """拼东财的 `filter`。

    长度注意：`(SECUCODE="600519.SH")(REPORT_DATE='2026-06-30')` 是 48 字符，
    `net.MAX_VALUE_LEN` 是 64 —— 这个余量是够的，但别往里加东西了。
    """
    text = f'(SECUCODE="{secu}")'
    if report_date:
        text += f"(REPORT_DATE='{report_date}')"
    return text


def _cache_path(secu: str, suffix: str, report_date: str | None) -> Path:
    key = urllib.parse.quote(f"{secu}_{suffix}_{report_date or 'LATEST'}", safe="")
    return CACHE_DIR / f"{key}.json"


def _fetch_rows(
    secu: str,
    suffix: str,
    report_date: str | None = None,
    use_cache: bool = True,
) -> list[dict]:
    """取一张表某个报告期的那一行（**唯一联网函数**）。

    返回 `[行]` 或 `[]`（空 = 这一期没数据，**不是错误**）。

    `report_date=None` 表示"最新一期"：不加 REPORT_DATE 过滤，
    按 REPORT_DATE 倒序取第一条。

    ## 连接会被掐断，而且掐断时不给任何响应

    实测：东财在频繁探测时会间歇性拒连（`RemoteDisconnected` / 空响应），
    和 curl 的 exit 52（Empty reply）是同一回事。这带来一个危险的歧义：

        拿不到数据可能是 (a) 代码不对 (b) 网络断了 (c) 被限流

    **所以这里不能静默返回"无数据"** —— 那会让调用方以为这家没有财务数据。
    处理方式：重试 3 次（指数退避 + 抖动）；仍然失败就抛错，把三种可能讲清楚。

    ## 缓存只写非空的响应

    200 但空有两种来源：真的没这一期，以及对方在限流下的异常响应。
    分不清，而缓存是"粘"的（一天内不会再问）—— 所以**空的绝不写缓存**，
    下次还会重新问一遍。代价是多几个请求，换来的是不会把一次异常固化成"没有数据"。
    """
    path = _cache_path(secu, suffix, report_date)

    if use_cache and path.exists():
        if (time.time() - path.stat().st_mtime) < CACHE_TTL:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = None
            if cached:
                return cached

    report_name = f"{REPORT_PREFIX}{suffix}"
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": _filter_for(secu, report_date),
        "pageNumber": 1,
        "pageSize": 1,
        "sortTypes": -1,
        "sortColumns": "REPORT_DATE",
        "source": "HSF10",
        "client": "PC",
    }
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    query = _query(
        {"secucode": secu, "report": report_name, "report_date": report_date or "latest"},
        purpose=f"A 股财报 {report_name}",
    )

    raw: bytes | None = None
    attempts = len(_RETRY_WAITS) + 1
    for attempt in range(attempts):
        _throttle()
        try:
            raw = net.guarded_get(
                url, query, timeout=30, headers={"User-Agent": USER_AGENT}
            )
            break
        except (OSError, http.client.HTTPException) as e:
            if attempt >= len(_RETRY_WAITS):
                raise CnFinancialsError(
                    f"查询 {secu} 的 {report_name} 时东财断开了连接"
                    f"（{type(e).__name__}），重试 {attempts} 次都没成功。\n"
                    f"三种可能，请核实是哪一种：\n"
                    f"  1. 这个代码在东财没有数据（不存在的代码常被直接掐连接）\n"
                    f"  2. 网络问题（本机访问国内站点应**直连**，不要挂代理）\n"
                    f"  3. 请求被限流（东财被频繁探测时会间歇性拒连；"
                    f"当前限速 {_rate_text()} 次/秒）\n\n"
                    f"**这不是「这家没有财务数据」** —— 失败与没有数据是两件事，"
                    f"不要据此把公司从可比集合里删掉。"
                ) from e
            wait = _RETRY_WAITS[attempt]
            # 抖动：±30%，避免几个请求退避后**同时**回来再一起撞限流
            time.sleep(wait * (0.85 + 0.3 * random.random()))

    if raw is None:                       # 理论上到不了这里（上面要么 break 要么 raise）
        return []

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CnFinancialsError(
            f"东财返回了无法解析的内容（{e}）。端点或参数可能改了 —— "
            f"**不把它当成「没有数据」**，那种错会静默污染结果。"
        ) from e

    rows = (payload.get("result") or {}).get("data") or []
    if not rows:
        return []

    if use_cache:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 缓存写不了不影响功能
    return rows


def detect_industry(secu: str, use_cache: bool = True) -> str | None:
    """判断一家公司用哪套模板 —— 按**资产负债表**探（理由见模块文档）。

    四套资产负债表模板实测互不重叠，所以第一个非空的就是答案。
    都空则返回 `None`（**不是错误**：可能这一期没披露，也可能不在覆盖内）。
    """
    for name in TEMPLATE_ORDER:
        _, balance_suffix = TEMPLATES[name]
        if _fetch_rows(secu, balance_suffix, None, use_cache=use_cache):
            return name
    return None


# ---------------------------------------------------------------------------
# 组装结果
# ---------------------------------------------------------------------------

def _to_float(value: object) -> float | None:
    """转数字。转不了返回 None（**不猜、不填 0**）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _pick(row: dict, candidates: tuple[str, ...]) -> tuple[str | None, float | None, str]:
    """按优先级取第一个**有值**的候选字段。

    返回 `(字段名, 数值, 问题说明)`；都没有则 `(None, None, "")`。

    **`0` 是有值** —— 只有「字段不存在」和「值是 None」算缺。
    """
    for field in candidates:
        if field not in row:
            continue
        raw = row[field]
        if raw is None:
            continue
        value = _to_float(raw)
        if value is None:
            return field, None, f"字段 {field} 的值 {raw!r} 不是数字，按缺处理（不猜）"
        return field, value, ""
    return None, None, ""


def _report_date_of(row: dict) -> str | None:
    """东财的 `REPORT_DATE` 是 `"2026-06-30 00:00:00"`，只要日期部分。"""
    raw = row.get("REPORT_DATE")
    if not raw:
        return None
    return str(raw)[:10]


def _blank_result(
    secu: str,
    industry: str | None,
    requested: str | None,
    reason: str,
) -> dict:
    """一个"什么都没取到"的结果。**不是异常** —— 缺就明说缺什么。"""
    templates = TEMPLATES.get(industry) if industry else None
    income_suffix, balance_suffix = templates if templates else (None, None)
    return {
        "code": _split_secucode(secu),
        "secucode": secu,
        "market": MARKET,
        "currency": CURRENCY,
        "industry": industry,
        "templates": {"income": income_suffix, "balance": balance_suffix},
        "requested_report_date": requested,
        "report_date": None,
        "source": "东方财富 datacenter（东财 F10 结构化报表）",
        "source_url": BASE,
        "values": {
            c.key: {"value": None, "field": None, "report_date": None,
                    "unit": "元", "table": None, "note": reason}
            for c in CONCEPTS
        },
        "gaps": [f"{c.key}：{reason}" for c in CONCEPTS],
        "notes": [PERIOD_NOTE, reason],
    }


def fetch(
    code: str,
    report_date: str | None = None,
    industry: str | None = None,
    use_cache: bool = True,
) -> dict:
    """取一家 A 股公司在某个报告期的财务数据。

    参数：

        code           6 位代码（`"600519"`）或带市场后缀（`"600519.SH"`）
        report_date    报告期 `YYYY-MM-DD`；**None = 最新一期**
        industry       显式指定模板行业（`"通用"/"银行"/"保险"/"证券"`），
                       跳过探测；不传则自动判断
        use_cache      用不用磁盘缓存（默认用）

    返回（结构固定，缺的值是 `None`，**不是 0**）：

        {
          "code": "600519", "secucode": "600519.SH",
          "market": "cn", "currency": "CNY",
          "industry": "通用",
          "templates": {"income": "GINCOME", "balance": "GBALANCE"},
          "requested_report_date": None,        # None = 要的是最新一期
          "report_date": "2026-06-30",          # **实际**取到的报告期
          "values": {
            "营业收入": {"value": 9.2278e10, "field": "TOTAL_OPERATE_INCOME",
                         "report_date": "2026-06-30", "unit": "元",
                         "table": "GINCOME", "note": ""},
            ...                                 # 共 CONCEPTS 那五项
          },
          "gaps": ["营业利润：…"],               # 缺的概念 + 原因
          "notes": ["…口径说明…", …],
        }

    **绝不**：拿 0 填缺、拿别的报告期填、拿另一个口径悄悄顶替。
    网络失败会抛 `CnFinancialsError`（那**不是**"没有数据"）。
    """
    secu = secucode(code)
    requested = normalize_report_date(report_date) if report_date else None

    if industry is None:
        industry = detect_industry(secu, use_cache=use_cache)
        if industry is None:
            return _blank_result(
                secu, None, requested,
                "四套模板（通用/银行/保险/证券）的资产负债表都取不到数据 —— "
                "可能是这家不在东财 F10 覆盖内、代码不对，或接口改了。"
                "**这不是网络失败**（网络失败会抛错，不会走到这里）。",
            )
    elif industry not in TEMPLATES:
        raise CnFinancialsError(
            f"不认识的行业模板 {industry!r} —— 用 {' / '.join(TEMPLATES)} 之一"
        )

    income_suffix, balance_suffix = TEMPLATES[industry]
    table_rows = {
        INCOME: _fetch_rows(secu, income_suffix, requested, use_cache=use_cache),
        BALANCE: _fetch_rows(secu, balance_suffix, requested, use_cache=use_cache),
    }
    suffixes = {INCOME: income_suffix, BALANCE: balance_suffix}

    row_of = {name: (rows[0] if rows else None) for name, rows in table_rows.items()}
    dates = {name: _report_date_of(row) for name, row in row_of.items() if row}
    # 报告期以利润表为准（四个概念里三个在利润表）；两表不一致时在 notes 里说清楚。
    period = dates.get(INCOME) or dates.get(BALANCE)

    notes = [PERIOD_NOTE]
    values: dict[str, dict] = {}
    gaps: list[str] = []

    for concept in CONCEPTS:
        row = row_of.get(concept.table) or {}
        field, value, problem = _pick(row, concept.candidates)

        if value is None:
            if row == {}:
                reason = (f"{suffixes[concept.table]} 在"
                          f"{requested or '最新一期'}取不到数据（这一期可能还没披露）")
            elif problem:
                reason = problem
            else:
                reason = (f"{suffixes[concept.table]} 里没有 "
                          f"{' / '.join(concept.candidates)}，或该期的值为空")
            values[concept.key] = {"value": None, "field": None, "report_date": None,
                                   "unit": "元", "table": None, "note": reason}
            gaps.append(f"{concept.key}：{reason}")
            continue

        values[concept.key] = {
            "value": value,
            "field": field,
            "report_date": dates.get(concept.table),
            "unit": "元",
            "table": suffixes[concept.table],
            "note": problem,
        }

    # 请求了某一期、回来的却是另一期 —— 这种"安静地换了期"必须说出来
    if requested:
        for name, got in dates.items():
            if got and got != requested:
                notes.append(
                    f"⚠️ 请求的报告期是 {requested}，{suffixes[name]} 返回的是 {got} —— "
                    f"结果里的数字**属于 {got}**，不是你要的那一期。"
                )
        if not dates:
            notes.append(
                f"⚠️ 报告期 {requested} 在利润表和资产负债表里都取不到数据 —— "
                f"该期可能还没披露（东财按报告期列示）。**没有回落到最近的另一期**："
                f"回退会给出一个报告期说不清的数字，而它看着完全正常。"
            )

    if dates.get(INCOME) and dates.get(BALANCE) \
            and dates[INCOME] != dates[BALANCE]:
        notes.append(
            f"⚠️ 利润表（{dates[INCOME]}）与资产负债表（{dates[BALANCE]}）报告期不一致。"
        )

    return {
        "code": _split_secucode(secu),
        "secucode": secu,
        "market": MARKET,
        "currency": CURRENCY,
        "industry": industry,
        "templates": {"income": income_suffix, "balance": balance_suffix},
        "requested_report_date": requested,
        "report_date": period,
        "source": "东方财富 datacenter（东财 F10 结构化报表）",
        "source_url": BASE,
        "values": values,
        "gaps": gaps,
        "notes": notes,
    }
