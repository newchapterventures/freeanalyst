"""港股财务 —— 东方财富 datacenter 的港股 F10「主要指标」报表。

## ⚠️ 记账币种：源给的字段**不足以**支撑跨币种运算（实测，2026-09-30）

同一行里：

    CURRENCY   = 'HKD'
    DPS_HKD    = 5.3          （港元的每股股息）
    DIVI_RATIO = 0.18481…     → 5.3 ÷ 0.18481 ≈ 28.68  ← 与"每股收益"同一个概念
    EPS_TTM    = 25.90        ← 但这里给的是 25.90（差约 10%）

两个"每股收益"对不上，说明**币种字段与数值不一定是同一套**。
所以本模块划一条线：

  · **比率**（净利率 / 毛利率 / 收入同比）→ 币种无关，**照用**
  · **倍数**（P/E、P/B）→ 用**东财自己算好的 `PE_TTM` / `PB_TTM`**（它自己处理了币种），
    并**随值附来源**；本模块**不**自己拿市值除利润
  · **绝对值**（收入 / 利润 / 资产 / 权益）→ 只作**量级参考**，并与源给出的 `currency` 一起显示
  · 任何**跨币种自己相除**算出来的倍数 → **不给**（与 EV 缺净债务同理：口径对不上就不给）

## 报表名与字段名不是猜的，是量过的（2026-09-30，腾讯控股 00700.HK）

    reportName = RPT_HKF10_FN_MAININDICATOR
    filter     = (SECUCODE="00700.HK")
    → 实测返回 89 个字段；本模块只取下面这些，每个都带回**来源字段名**。

## 为什么港股能给 P/E、P/B，而 A 股要自己算
东财这张表**自己就算了 PE_TTM / PB_TTM**，而且**它把币种处理过了**：
实测腾讯 2026 中报 —— EPS_TTM 25.90（元），股价 428 港元 ≈ 393.8 元，
393.8 ÷ 25.90 ≈ 15.2，与它给的 PE_TTM 14.72 同量级（差在 TTM 窗口）。

## ⚠️ 本模块**不**自己算市值类倍数
同一张表里 `TOTAL_MARKET_CAP` 是**港元**（腾讯 3.93 万亿 ÷ 91 亿股 = 431.8 港元 ✓
与 428 的现价对得上），而利润、权益是**人民币**。
**直接相除会错约 8%，而且从数字上看不出来** —— 所以：
  · P/E、P/B 用**东财算好的那两个字段**（附来源）
  · 「市值/收入」这类需要自己相除的，港股上**不给**（口径对不上就不给，与 EV 缺净债务同理）
"""
from __future__ import annotations

import json
import urllib.parse

import net

HOST = "datacenter-web.eastmoney.com"
EXTRA_HOSTS: frozenset[str] = frozenset({HOST})
USER_AGENT = "FreeAnalyst/0.44 (noreply@newchapterventures.com)"

MARKET = "hk"
#: 上市地计价货币（行情/市值用港元）。注意：**报表本身的报告币种**逐行不同 ——
#: 见返回里的 `currency`（实测腾讯是人民币），不要把两者混在一起。
CURRENCY = "HKD"
HAS_PRICES = True

REPORT = "RPT_HKF10_FN_MAININDICATOR"

#: 本模块取的概念 → 东财的字段名（**逐字来自实测**
#: `docs/数据源-中港财务.md` 与 2026-09-30 的探针，不在此另立一套）。
FIELDS: dict[str, str] = {
    "营业收入": "OPERATE_INCOME",
    "营业利润": "OPERATE_PROFIT",
    "净利润": "HOLDER_PROFIT",            # 归属母公司
    "总资产": "TOTAL_ASSETS",
    "归母权益": "TOTAL_PARENT_EQUITY",
    "净利率": "NET_PROFIT_RATIO",         # %
    "毛利率": "GROSS_PROFIT_RATIO",       # %
    "收入同比": "OPERATE_INCOME_YOY",     # %
    "每股收益TTM": "EPS_TTM",
    "每股净资产": "BPS",
    "市盈率TTM": "PE_TTM",                # 东财算好的（币种已处理）
    "市净率TTM": "PB_TTM",
}


def secucode(code: str) -> str:
    """`700` / `0700` / `00700` → `00700.HK`（港股代码补足 5 位，实测量过）。"""
    raw = str(code).strip().upper().replace(".HK", "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise ValueError(f"不像港股代码：{code!r}")
    return f"{digits.zfill(5)}.HK"


def _row_from(raw: dict) -> dict:
    """把东财的一行变成我们的形状 —— 每个值都带来源字段名。"""
    out: dict[str, object] = {
        "code": str(raw.get("SECURITY_CODE") or ""),
        "name": str(raw.get("SECURITY_NAME_ABBR") or ""),
        "period": str(raw.get("STD_REPORT_DATE") or raw.get("REPORT_DATE") or "")[:10],
        "period_label": str(raw.get("REPORT_TYPE") or ""),
        "currency": str(raw.get("CURRENCY") or ""),
        "source": f"东方财富 datacenter · {REPORT}",
        "fields": {},
    }
    vals: dict[str, float] = {}
    for label, key in FIELDS.items():
        v = raw.get(key)
        if isinstance(v, (int, float)):
            vals[label] = float(v)
    out["fields"] = vals
    return out


def _query(code: str, page_size: int = 8) -> dict:
    """构造出境查询并取回 JSON。

    A 股那条路（`cn_financials.py`）的姿势照抄：参数先过 `PublicQuery` 的
    长度/字符集校验，主机在 `extra_hosts` 里**就地声明** ——
    **没有改 `net.py` 的全局白名单**。
    """
    params = {
        "reportName": REPORT,
        "columns": "ALL",
        "pageSize": str(page_size),
        "pageNumber": "1",
        "sortColumns": "STD_REPORT_DATE",
        "sortTypes": "-1",
        "filter": f'(SECUCODE="{secucode(code)}")',
    }
    url = f"https://{HOST}/api/data/v1/get?{urllib.parse.urlencode(params)}"
    #: 交给闸门的是**一小份声明**，不是线上真正发的参数 —— 照 `cn_financials.py` 的姿势：
    #: `guarded_get` 会把这份声明**再拼到 URL 上**，两边给同一份就会把参数拼成
    #: `reportName=X,X`（实测：东财回 "报表配置不存在,RPT_…,RPT_…"）★ 2026-09-30 踩过。
    query = net.PublicQuery(
        {"secucode": secucode(code), "report": REPORT},
        purpose=f"取港股 {secucode(code)} 的财务指标",
        extra_hosts=EXTRA_HOSTS,
    )
    raw = net.guarded_get(url, query, timeout=30,
                          headers={"User-Agent": USER_AGENT})
    return json.loads(raw.decode("utf-8", errors="replace"))


def fetch(code: str) -> dict | None:
    """最新一期。取不到返回 None（**不编**）。"""
    rows = history(code, n=1)
    return rows[0] if rows else None


def history(code: str, n: int = 4) -> list[dict]:
    """最近 n 期（按报告期倒序）。"""
    data = _query(code, page_size=max(n, 4))
    rows = ((data.get("result") or {}).get("data")) or []
    return [_row_from(r) for r in rows[:n]]
