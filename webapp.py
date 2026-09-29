#!/usr/bin/env python3
"""本地网页 —— 把 `appraise` 那条命令行搬到浏览器里。

    python3 freeanalyst.py ui ~/deals/某个标的/
    python3 webapp.py ~/deals/某个标的/ --port 8765

## 为什么用标准库起服务（而不是 FastAPI / Flask）

这个项目的底线是**零第三方依赖**（唯一的例外是 PDF 解析的 pdfplumber，
而且不装也能跑）。为了一个本地单用户的界面引一个 Web 框架进来，
会把"能审计"这件事变模糊 —— 多一个依赖就多一份不明代码。
`http.server` 够用：一次一个人用、只在本机、每步都是同步的。

## 只绑 127.0.0.1

**不绑 0.0.0.0。** 绑了的话，同一个 WiFi 下任何人都能打开这个页面、
进而读到你这台机器上的尽调材料。这条不是偏好，是红线 ——
和 `guard.py` 只放行回环是同一类设计。

## 六步向导，这一版做到哪

设计稿见 `docs/interface-mockup.html`（六步：丢材料 / 提取核对 / 行业建议 /
可比公司 / 假设清单 / 结论）。**这一版做到第 1、2、5、6 步**，
中间两步（行业建议、可比公司选取）需要先把流程层接进来，还没做。
页面上会照实说明 —— 不做成"看起来能用"。

## 每一步背后都是一个已经存在的函数

    第 1、2 步   intake.scan()     认材料、判单位口径、跑勾稽
    第 5 步      intake.questions() 把必须由人给的假设列出来
    第 6 步      value.run_report() 出报告（材料目录驱动，同一条报告流）

界面不重写任何判断逻辑。**它只是一层皮** —— 底下是谁算的、怎么算的，
跟命令行那条路完全一样。
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import intake
from intake import Answer, Materials, parse_answer

HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: 接口清单与版本 —— 页面拿它跟自己对表。
#: **真踩过**：页面加了「选择文件夹…」，但跑着的服务还是旧进程（Python 代码不会热加载），
#: 于是点下去只回一句 `unknown endpoint`。现在页面能自己发现这件事并说清楚。
VERSION = "0.42"
ENDPOINTS = ("health", "scan", "appraise", "pick", "config", "gate", "cloud-check",
             "pull", "pull-status", "model-check", "onboarded", "comps", "ask")
#: 页面依赖的**能力**标记（比接口更细一层：同一个接口也可能少字段）。
#: 页面会逐条核对，缺哪条就提示"服务是旧进程"。
#: 真踩过：百分比字段加进引擎后没重启服务，页面上那些框**静默地没有 %** ——
#: 用户于是不知道填 5、0.05 还是 5%。
FEATURES = ("percent-unit", "llm-config", "install-model", "onboard-state", "comps-step")


#: 从报表单位里读**标的在哪个市场** —— 单位字符串里带币种（实测："千美元"/"人民币千元"）。
#: 这不是猜：币种是财报自己写的口径，读它是**用它自己的话说它自己**。
_MARKET_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("美元", "usd", "us$"), "us"),
    (("港元", "港币", "hkd", "hk$"), "hk"),
    (("人民币", "rmb", "cny", "元"), "cn"),
)


def _market_of(unit: str) -> str:
    """报表单位 → 标的所在市场（`us` / `hk` / `cn` / `""` 判不出）。"""
    low = (unit or "").lower()
    if not low:
        return ""
    for words, market in _MARKET_HINTS:
        if any(w in low for w in words):
            return market
    return ""


#: 市场名（给提示文案用）
MARKET_NAME = {"us": "美股", "hk": "港股", "cn": "人民币（A 股 / 港股）"}


def _comps_market_warning(target_unit: str, peer_market: str = "us") -> str:
    """标的与同行的**市场不一致**时必须说清后果。

    ## 为什么这条不能省（专业上是对的，用户提的）
    · **beta 对应的是各自市场的指数** —— 拿美股同行的 β 去算 A 股标的的 WACC，
      等于把两个市场的系统性风险混在一起；
    · **不同市场的估值中枢本来就不同** —— A 股 / 港股 / 美股的 PE、EV/EBITDA
      水平系统性有差，**跨市场套倍数会带进一个方向不明的偏差**。

    所以这里**不是**给一个"仅供参考"的软话，而是说清"这一步的输出**不能**怎么用"，
    再给两条可走的路。
    """
    tm = _market_of(target_unit)
    if not tm or tm == peer_market:
        return ""
    return (f"⚠ 口径不一致：**标的的报表是{MARKET_NAME.get(tm, tm)}口径**，"
            f"而这一步取的是**{MARKET_NAME.get(peer_market, peer_market)}同行**。\n"
            f"  · beta 对应的是**各自市场的指数** —— 跨市场套用等于把两边的系统性风险混在一起；\n"
            f"  · 不同市场的**估值中枢本来就不同**（PE / EV·EBITDA 系统性有差），"
            f"跨市场套倍数会带进一个**方向不明的偏差**。\n"
            f"  两条可走的路：① **乘数**用同市场同行的倍数（在第 5 步手填，工具不替你定）；"
            f"② **beta** 取同市场同业的去杠杆 β。\n"
            f"  （同行基本面本身仍然可看：增速、利润率、规模是**公司属性**，"
            f"跨市场对照有意义；倍数不是。）")


#: 市场 → 该市场的报表币种。**只用于在用户没说时补一个默认**，
#: 不是断言（港股里人民币报表很常见，所以：声明 > 币种线索 > 这个默认）。
_MARKET_CURRENCY = {"us": "USD", "cn": "CNY", "hk": "HKD"}


def _comps_scope(target_market: str, target_unit: str, peer_market: str,
                 peer_currency: str = "", target_currency: str = "") -> dict:
    """第 4 步的**市场层**：谁和谁能同台比、哪一半能用。

    这是"覆盖中/美/港"的地基 —— 三个市场就要有多个源，而**跨市场的对照必须分级**：

    | 比什么 | 同市场 | 跨市场 |
    |---|---|---|
    | **基本面比率**（增速 / 利润率） | ✓ | ✓ **公司属性**，跨市场有意义 |
    | **绝对规模**（收入是多少钱） | ✓ | ✗ 币种不同，数不能并列 |
    | **倍数**（PE / EV·EBITDA）与 **beta** | ✓ | ✗✗ 各自绑定市场的指数与估值中枢 |

    所以这里返回的是**许可**（谁能用），不是一句提示 —— 后面的倍数路径要靠它拦，
    免得哪天接了价格源，跨市场的倍数**静默**混进结论里。

    ## 目标市场：**用户声明的优先**，其次按币种推断
    实测理由：港股里**人民币报表很常见**（内地企业在港上市），所以"报表是人民币"
    推不出"它在 A 股" —— 币种只是**线索**，声明才是**依据**。两者不一致时说清楚。
    """
    declared = (target_market or "").strip().lower()
    inferred = _market_of(target_unit)
    market = declared or inferred
    same_market = bool(market) and market == peer_market
    # 币种：今天只有一个源（USD），所以同行币种取源的声明。
    # 标的币种：用户给的优先；没给就从**市场**补一个默认（人民币报表 → CNY）；
    # 市场也判不出来才算"不知道"（不知道就不许断言币种相同）。
    # ⚠ 这个补法是实测踩出来的：只传了 `target_unit` 不传 `target_currency` 时，
    #   `same_currency` 会被算成 True，于是**跨币种的绝对规模也放行了** ✗。
    cur_peer = (peer_currency or "").upper()
    cur_target = (target_currency or "").upper() or _MARKET_CURRENCY.get(market, "")
    same_cur = (not cur_target) or (not cur_peer) or cur_target == cur_peer
    notes: list[str] = []
    if declared and inferred and declared != inferred:
        notes.append(
            f"你声明标的是{MARKET_NAME.get(declared, declared)}，"
            f"但报表口径看着像{MARKET_NAME.get(inferred, inferred)}"
            f"（港股里人民币报表很常见）—— **按你的声明走**，这里记录一下。")
    if not market:
        notes.append("标的的市场判不出来（报表单位里没有币种线索，你也没声明）—— "
                     "所以下面**不假设**它和同行同市场。")
    return {
        "target_market": market,
        "target_market_declared": declared,
        "target_market_inferred": inferred,
        "peer_market": peer_market,
        "target_currency": cur_target,
        "peer_currency": cur_peer,
        "same_market": same_market,
        "same_currency": same_cur,
        # ①② 基本面比率永远可用；绝对规模要币种一致
        "ratios_allowed": True,
        "scale_allowed": same_cur,
        # ③ 倍数与 beta：**硬条件是同市场 + 同币种**
        "multiples_allowed": bool(same_market and same_cur),
        "notes": notes,
    }


#: 只要**市值**就能算的倍数 —— 价格源到位即可全部打通。
PRICE_MULTIPLES: dict[str, str] = {
    "pe": "P / E", "pb": "P / B", "price_to_revenue": "市值 / 营业收入",
}

#: 需要**企业价值（EV）**的倍数 —— 还差一样东西：**同行的净债务**。
#: EV = 市值 + 净债务，而 EDGAR 里没有统一的债务/现金科目口径（各家拆法不同，
#: 有的把租赁、可转债拆成好几行）。**缺就明说，绝不拿市值代替 EV** ——
#: 那是最不容易看出来的错：数字合理、结论全偏。
EV_MULTIPLES: dict[str, str] = {
    "ev_ebitda": "EV / EBITDA", "ev_ebit": "EV / EBIT", "ev_revenue": "EV / 营业收入",
}

#: 所有"需要市值口径"的倍数（含暂时算不了的）—— 用来把报错说得准确。
MULTIPLE_METRICS: dict[str, str] = {**PRICE_MULTIPLES, **EV_MULTIPLES}


def api_comps(payload: dict) -> dict:
    """第 4 步 · 可比公司（**只做基本面对照**）。

    ## 边界：这一步**不假装**能给倍数（实测文档）
    `datasources/sec_edgar.py` 的头注释写得很清楚：**EDGAR 有财报、没有股价** ——

        能算：EBITDA、收入、净利、总资产、股数
        不能算：市值、EV、EV/EBITDA   ← 都需要价格
        "要做市值的倍数，必须再配一个价格源。" —— 那个源没接。

    所以这里给的是**同行在基本面指标上的分布**（收入增速 / EBITDA 率 / 收入规模），
    用来支撑假设与假设参谋 —— **不是倍数**。倍数那一步等价格源。

    ## 纪律（`valuation/comps_workflow.py`）
    · **≥3 家才给中位数当结论**，不够就明说不够（2 家的"中位数"没有意义）
    · 代码在 SEC 查不到 → **明说只支持美股** + 给退路（留空不凑参照系）
    · 不代用户选指标：指标由用户选，这里只算
    """
    from datasources import sec_edgar as se
    from valuation import advisor as ad
    from valuation.comps_workflow import MIN_COMPS

    raw = payload.get("tickers") or ""
    # 逗号、分号、顿号、空格、换行全当分隔符 —— 用户从别处粘贴过来什么形状都有。
    # ⚠️ **全角也要拆**：中文输入法打出来的是「，」「；」「、」，实测漏掉全角逗号时
    # 「LEA，MGA」会被当成一个代码，然后报"SEC 查不到"（错怪用户）。
    tickers = [t.strip().upper()
               for t in str(raw)
               .replace(",", " ").replace("，", " ")
               .replace(";", " ").replace("；", " ")
               .replace("、", " ").replace("　", " ")
               .split()
               if t.strip()]
    if not tickers:
        return {"ok": False,
                "error": "先给可比公司代码（美股，逗号分隔，如 LEA, MGA, BWA）"}

    metric = (payload.get("metric") or "revenue_cagr").strip()
    target_unit_early = payload.get("target_unit") or ""
    scope_early = _comps_scope(payload.get("target_market") or "", target_unit_early,
                               se.MARKET, se.CURRENCY,
                               payload.get("target_currency") or "")
    if metric in MULTIPLE_METRICS:
        # 三类原因要分开说 —— 说错了用户会去改错的地方
        if not scope_early["multiples_allowed"]:
            return {"ok": False, "error": (
                f"「{MULTIPLE_METRICS[metric]}」不能跨市场套用：标的是"
                f"{MARKET_NAME.get(scope_early['target_market'], '未声明')}、"
                f"同行在{MARKET_NAME.get(se.MARKET, se.MARKET)} —— "
                f"倍数各自绑定市场的指数与估值中枢，跨市场会带进方向不明的偏差。"
                f"请用**同市场**同行，或改用基本面比率。")}
    is_multiple = metric in MULTIPLE_METRICS
    if not is_multiple and metric not in ad.METRIC_FUNCS:
        usable = "、".join(list(ad.METRIC_FUNCS) + list(MULTIPLE_METRICS))
        return {"ok": False, "error": f"不支持的指标：{metric}（可用：{usable}）"}
    years = int(payload.get("years") or 3)
    as_of = (payload.get("as_of") or "").strip() or None

    pairs: list[tuple[str, str]] = []
    missing: list[str] = []
    for t in tickers:
        cik = se.ticker_to_cik(t)
        if cik:
            pairs.append((cik, t))
        else:
            missing.append(t)
    if missing:
        return {"ok": False, "error":
                f"这些代码在 SEC 查不到 → {'、'.join(missing)}。"
                "**目前只支持美股** —— A 股/港股还没有免接口的数据源。"
                "（留空不是缺陷：拿不到同行分布时，不凑一个参照系才是对的。）"}

    if is_multiple:
        # 市值类倍数：**市值 = 基准日收盘价 × 股数**，分母在同一份财报里。
        # 三样东西必须齐：代码（价格源要）、CIK（财报要）、基准日（两边都要）。
        stat = ad.build_peer_multiples(
            [(cik, code, code) for cik, code in pairs],
            metric=metric, as_of=as_of, market=se.MARKET, unit=se.CURRENCY)
        title, fmt = MULTIPLE_METRICS[metric], ad.MULTIPLE_FUNCS[metric][1]
    else:
        stat = ad.build_peer_stat(pairs, metric=metric, years=years, as_of=as_of)
        _, title, fmt = ad.METRIC_FUNCS[metric]
    # 市场与币种**由源自己声明** —— 不许在这里写死（接中/港源时全靠这个）
    peer_market = se.MARKET
    target_unit = target_unit_early
    scope = scope_early
    # 绝对规模（收入是多少钱）跨币种**不能直接并列**：同行分布自身是同一币种、有效，
    # 但"标的 vs 它们"要先换算。比率（增速/利润率）没这个问题。
    scale_note = ""
    if metric == "revenue_scale" and not scope["same_currency"]:
        scale_note = (f"⚠ 口径：这一列是**绝对金额**，同行是{scope['peer_currency']}、"
                      f"标的是{scope['target_currency'] or '另一种币种'} —— "
                      f"**同行分布自身有效**（同一币种），但标的与它们比规模**要先换算币种**。")
    return {
        "ok": True, "metric": metric, "title": title, "fmt": fmt,
        "unit": stat.unit, "n": stat.n, "min_comps": MIN_COMPS,
        "enough": stat.n >= MIN_COMPS,
        "peer_market": peer_market,
        "target_market": scope["target_market"],
        # 市场层：谁和谁能同台比、哪一半能用（倍数路径以后靠 multipliers_allowed 拦）
        "scope": scope,
        "scale_note": scale_note,
        # 标的和同行**不在同一个市场**时，说清这一步的输出**不能**怎么用
        "market_warning": _comps_market_warning(target_unit, peer_market),
        "rows": [{"label": lab, "value": v}
                 for lab, v in zip(stat.labels, stat.values)],
        "p25": stat.quantile(0.25), "median": stat.median(), "p75": stat.quantile(0.75),
        "lo": min(stat.values) if stat.values else None,
        "hi": max(stat.values) if stat.values else None,
        "source": stat.source,
        "gaps": stat.gaps,
        # 每家的取数依据（价格哪一天 × 股数哪一天 ÷ 分母哪个期末）——
        # 市值类倍数把两个世界拼在一起，**每个时点都要答得出来**。
        "basis": stat.basis,
    }


#: 项目根目录 —— 页面正文和默认输出都相对它。
ROOT = Path(__file__).resolve().parent

#: 页面正文放在单独文件里 —— **线上那一页和设计稿是同一个文件**。
#: 好处不只是省事：不存在「稿子改好看了、实现没跟上」这种分岔。
#: `webapp_page.html` 可以直接双击打开看样式（没有后端时它自己进设计预览模式）。
PAGE_PATH = ROOT / "webapp_page.html"
#: 说明文件（功能 / 免责声明 / 模型 / 使用 / 调试 / 接自己的大模型）—— 独立成页，
#: 因为它给的是**使用者**，而 README 给的是开发者/审计者，两边读者不同。
DOC_PATH = ROOT / "webapp_doc.html"
#: 模型配置页（本机运行时 / 云端服务商 / 用途绑定 / 出网审计）。
CONFIG_HTML_PATH = ROOT / "webapp_config.html"
#: 除接口之外还提供哪些页面 —— 页面据此判断「服务是不是旧进程」
PAGES = ("doc", "config")


def doc_html() -> str:
    """说明文件页。缺失时给一句人话，不抛 500。"""
    if DOC_PATH.exists():
        return DOC_PATH.read_text(encoding="utf-8")
    return ("<!DOCTYPE html><meta charset='utf-8'>"
            "<body style='font:14px monospace;background:#05070A;color:#D7F5E9;padding:40px'>"
            "说明文件缺失：webapp_doc.html 不在仓库里。</body>")


def config_html() -> str:
    """模型配置页。缺失时给一句人话，不抛 500。"""
    if CONFIG_HTML_PATH.exists():
        return CONFIG_HTML_PATH.read_text(encoding="utf-8")
    return ("<!DOCTYPE html><meta charset='utf-8'>"
            "<body style='font:14px monospace;background:#05070A;color:#D7F5E9;padding:40px'>"
            "配置页缺失：webapp_config.html 不在仓库里。</body>")


def page_html() -> str:
    """要发出去的页面。**优先读 `webapp_page.html`**，没有就退回内置那份。

    在页面上改动频繁的时候，单独一个 html 文件比在 Python 字符串里改要好得多：
    能直接双击看、能用编辑器的语法高亮、diff 也看得清。
    """
    if PAGE_PATH.exists():
        return PAGE_PATH.read_text(encoding="utf-8")
    return PAGE

#: 这次会话扫过的材料（`path → Materials`）。**扫一次就够** ——
#: PDF 走一遍 OCR 可能要几分钟，点一下重扫一遍没人受得了。
_CACHE: dict[str, Materials] = {}


def pick_path(kind: str = "dir") -> dict:
    """让**操作系统**弹一个原生选择框，把真实路径返回给页面。

    ## 为什么不是浏览器的文件选择框（`<input type=file>`）

    浏览器出于沙箱，`<input type=file>` 只给文件名，**拿不到真实路径**；
    想拿到内容就只能把文件**上传**一份 —— 那等于把机密材料复制到别处去，
    对一个「材料不出本机」的工具是不能接受的。

    而我们的服务本来就跑在这台机器上，所以让它去调系统的选择框：
    用户选完，直接拿到真实路径，**一个字节都不复制**，材料还在原地。

    只做 macOS（`osascript`）。其他系统返回一句人话，让人手工填路径 ——
    不做「看起来能用」的假按钮。
    """
    if sys.platform != "darwin":
        return {"ok": False,
                "error": f"原生选择框只做了 macOS（当前系统 {sys.platform}）—— "
                         "请直接填路径，或把文件夹从访达拖进页面"}
    prompt = "选择尽调材料目录" if kind == "dir" else "选择材料文件"
    script = (f'POSIX path of (choose folder with prompt "{prompt}")' if kind == "dir"
              else f'POSIX path of (choose file with prompt "{prompt}")')
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True,
                             text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "选择框等待超时"}
    except OSError as exc:
        return {"ok": False, "error": f"调不起系统选择框：{exc}"}
    if out.returncode != 0:
        # 用户按了取消。**取消不是错误** —— 界面上不该弹红字。
        return {"ok": False, "cancelled": True}
    path = out.stdout.strip().rstrip("/") or "/"
    return {"ok": True, "path": path}


# ─────────────────────── 接口层：把现有函数包成 JSON ───────────────────────

def api_ask(payload: dict) -> dict:
    """对话面板的后端 —— **当前不接模型**（骨架先立住）。

    ## 它现在只会两件事（都是纯代码）
    ① `search`  —— 在材料里按关键词检索，回**文件 + 页码 + 原文片段**
    ② `propose` —— 把一句话转成**结构化提议**（哪个字段、什么值、来源是谁），
                   **等用户点头才生效**（不点头就什么都不发生）

    ## 为什么先这样
    模型来了之后，"读懂一段话"接在**同一处**：位置、痕迹、授权流程都不用改。
    在那之前，这个面板**不是不能互动**，只是互动**不智能** —— 它会明说这一点，
    而不是装作听懂了。

    ## 三条硬约束（对话层同样要守）
    · **对话只产生输入与判断，永不产生数字** —— 提议要被采纳才落进字段，
      数字仍由引擎算（"算术不能有随机"）
    · **材料不出本机** —— 检索在本地材料上做，不发任何请求
    · **可追溯** —— 提议带 `source="对话输入（未核实）"` + 置信度「低」，
      采纳后写进字段的注释里，报告里能看回来
    """
    action = (payload.get("action") or "").strip()
    path = (payload.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "先在第一步载入材料 —— 对话要用它来检索"}

    if action == "search":
        return _ask_search(path, (payload.get("q") or "").strip(),
                           int(payload.get("limit") or 20))
    if action == "propose":
        return _ask_propose(path, (payload.get("text") or "").strip())
    if action == "plan":
        filled = payload.get("filled") or []
        if isinstance(filled, dict):        # 页面传字典也行，只取键
            filled = list(filled.keys())
        return _ask_plan(path, {str(k) for k in filled})
    return {"ok": False, "error": f"不认识的动作：{action}（可用：search / propose / plan）"}


#: 一句话里的常见说法 → **问题清单里标签的片段**。
#: ⚠️ **不要写死键名**：清单里的键是 `da_pct_revenue` 这种，猜的名字（`da_ratio`）
#: 一漂就失效 —— 实测「折旧摊销 12%」因此认不出来 ✗。按**标签**匹配才不会漂。
#: 长词放前面（"永续增长"要先于"增长"匹配）。
_ASK_ALIASES: tuple[tuple[str, str], ...] = (
    ("永续增长", "永续增长"), ("退出倍数", "退出倍数"),
    ("净债务", "净债务"), ("有息负债", "有息负债"),
    ("所得税", "所得税"), ("税率", "所得税"),
    ("无风险", "无风险"), ("溢价", "风险溢价"),
    ("beta", "beta"), ("贝塔", "beta"),
    ("债务成本", "债务成本"), ("借款利率", "债务成本"),
    ("折旧摊销", "折旧摊销"), ("资本开支", "资本开支"),
    ("营运资本", "营运资本"), ("增长率", "增长"),
    # 「ebitda」那条**删掉了**：真实清单里没有含 EBITDA 的**标签**
    # （只有「乘数用的指标」这类，指标名在选项里不在标签里）→ 留着也永远匹配不上，
    # 只会让人以为"说 EBITDA 就能识别"。核对脚本：`/tmp/check_aliases.py`。
)


#: 「判断」类项按**对估值的影响**排序 —— 表短、可审阅，每条都写明"为什么先问它"。
#: 按**标签片段**匹配（和 `_ASK_ALIASES` 同一套办法，不写死键名 —— 键会漂）。
_ASK_IMPACT: tuple[tuple[str, str], ...] = (
    ("增长", "收入增长是 DCF 的主驱动，敏感性网格第一个动的就是它"),
    ("利润率", "利润率决定 EBITDA 基数 —— 乘数法和 DCF 都吃它"),
    ("ebitda", "利润率决定 EBITDA 基数 —— 乘数法和 DCF 都吃它"),
    ("折旧摊销", "EBITDA = 营业利润 + 折旧摊销；缺它，两个比率一起空掉"),
    ("资本开支", "资本开支占收入比直接决定自由现金流能不能为正"),
    ("营运资本", "营运资本的增量会吃掉增长里的现金，高增长公司尤其明显"),
    ("净债务", "净债务偏低会让股权价值偏高 —— 漏掉它是最危险的漏项"),
    ("永续增长", "终值占 DCF 大头，而且有硬上界（长期名义 GDP 增速）"),
    ("折现率", "WACC 是最敏感的输入之一，动一点结论就动一截"),
    ("税率", "有效税率影响税后现金流"),
)


def _ask_why(q: intake.Q, origin: str) -> str:
    """这一项**为什么现在问**、以及它从哪来。让人不用猜对话的次序。"""
    if origin == intake.ORIGIN_DERIVED:
        return "报表里能推算出来 —— 工具已给出参考值，确认或改一个数即可"
    if origin == intake.ORIGIN_EXTERNAL:
        return "这是公司之外的市场信息（工具不提供、也不该编），需要你查一下"
    if origin == intake.ORIGIN_FILING:
        return "报表里直接有的数 —— 一般扫描时已填好，核对即可"
    label = getattr(q, "label", "") or ""
    for frag, why in _ASK_IMPACT:
        if frag in label or frag.lower() in label.lower():
            return why
    return "你对未来的判断 —— 报表不涉及，只能由你给"


def _impact_rank(q: intake.Q) -> int:
    """这一项在**影响表**里的次序（表外的排最后）。用于稳定排序。"""
    label = (getattr(q, "label", "") or "").lower()
    for i, (frag, _why) in enumerate(_ASK_IMPACT):
        if frag in label or frag.lower() in label:
            return i
    return len(_ASK_IMPACT)


def _ask_order(qs: list[intake.Q], filled: set[str]) -> list[tuple[intake.Q, str]]:
    """还差哪些 + **先问哪个** + 为什么是这个次序。

    ## 排序依据是**用户的成本**，不是"重要性"的玄学
      ① `财报推算` —— 工具已经有参考值，用户只需点头 = **零成本** → 先清掉
      ② `判断`     —— 再分两小步：
                     a. **点一下就好**的（有 options/suggest，如"估值目的""立场"）→ 便宜，先问
                     b. 要动脑的**数值项** → 按影响表排（见 `_ASK_IMPACT`）
                     ⚠️ 这一层**显式写出来**：实测它原先只是"清单顺序的巧合"——
                        估值目的那几项恰好排在前面 —— 换个清单就不成立了 ✗
      ③ `外部`     —— 要去查资料（外部作业）→ 放最后，因为它会打断思路
      ④ `财报`     —— 报表里直接有的，扫描时一般已填好
    排序**稳定**：同档同秩保持清单原顺序（每次问的次序都一样，可预期）。
    """
    buckets: dict[str, list[intake.Q]] = {
        intake.ORIGIN_DERIVED: [], intake.ORIGIN_JUDGMENT: [],
        intake.ORIGIN_EXTERNAL: [], intake.ORIGIN_FILING: [],
    }
    for q in qs:
        key = getattr(q, "key", "")
        if not key or key in filled:
            continue
        buckets.setdefault(getattr(q, "origin", "") or "", []).append(q)

    out: list[tuple[intake.Q, str]] = []

    def take(origin: str) -> None:
        out.extend((q, _ask_why(q, origin)) for q in buckets.get(origin, []))

    take(intake.ORIGIN_DERIVED)

    judge = buckets.get(intake.ORIGIN_JUDGMENT, [])
    pickable = [q for q in judge
                if getattr(q, "options", ()) or getattr(q, "suggest", ())]
    thoughtful = [q for q in judge if q not in pickable]
    thoughtful.sort(key=_impact_rank)          # 稳定：同秩保持原顺序
    for q in pickable + thoughtful:
        out.append((q, _ask_why(q, intake.ORIGIN_JUDGMENT)))

    take(intake.ORIGIN_EXTERNAL)
    take(intake.ORIGIN_FILING)
    return out


def _ask_plan(path: str, filled: set[str]) -> dict:
    """第 5 步的**问答计划**：还差哪些、先问哪个、为什么是这个次序。

    `filled` **由页面传**（页面上已经填了哪些键）—— 服务端**不猜**用户在页面上填了什么。
    每项都带 `reference`（材料里推出来的参考值）与 `origin`，界面据此决定
    「沿用参考」还是「要你给」。
    """
    mat = _CACHE.get(path)
    if mat is None:
        return {"ok": False, "error": "这份材料还没载入过 —— 先在第一步解析它"}

    qs = [q for q in intake.questions(mat) if getattr(q, "key", "")]
    order = _ask_order(qs, filled)

    def pub(q, why: str) -> dict:
        return {"key": q.key, "label": q.label, "hint": q.hint, "unit": q.unit,
                "origin": q.origin, "reference": q.reference, "group": q.group,
                "options": list(getattr(q, "options", ()) or ()),
                "suggest": list(getattr(q, "suggest", ()) or ()),
                "why": why}

    return {"ok": True, "total": len(qs),
            "filled": len([q for q in qs if q.key in filled]),
            "missing": len(order),
            "next": pub(*order[0]) if order else None,
            "order": [pub(q, why) for q, why in order]}


def _ask_search(path: str, q: str, limit: int = 20) -> dict:
    """在材料里按关键词找 —— **纯检索，不发任何请求，材料不出本机**。"""
    from ingest import pdf as ip        # 延迟导入：只在真要读 PDF 时才需要
    if len(q) < 2:
        return {"ok": False, "error": "关键词太短（至少 2 个字）—— 太短会命中一大片"}
    mat = _CACHE.get(path)
    if mat is None:
        return {"ok": False, "error": "这份材料还没载入过 —— 先在第一步解析它"}
    files = sorted(p for p in Path(mat.directory).rglob("*") if p.is_file())
    if not mat.is_file:
        files = [f for f in files if f.suffix.lower() in (".pdf", ".txt", ".md")]
    hits: list[dict] = []
    total = 0
    for f in files:
        suffix = f.suffix.lower()
        if suffix not in (".pdf", ".txt", ".md"):
            continue
        try:
            if suffix == ".pdf":
                doc = ip.extract_pdf(f)
                pages = [(pg.number, pg.raw_text or pg.text or "") for pg in doc.pages]
            else:
                # 纯文本材料没有页码概念 —— **不编页码**，如实给 None。
                pages = [(None, f.read_text(encoding="utf-8", errors="ignore"))]
        except Exception:                                   # noqa: BLE001
            continue
        for num, text in pages:
            if q not in text:
                continue
            total += 1
            if len(hits) >= limit:
                continue
            i = text.find(q)
            snippet = text[max(0, i - 60): i + 90].replace("\n", " ")
            hits.append({"file": f.name, "page": num, "snippet": snippet})
    return {"ok": True, "q": q, "hits": hits, "total": total,
            "truncated": total > len(hits)}


def _ask_propose(path: str, text: str) -> dict:
    """把一句话转成**结构化提议**。**不猜就明说猜不出。**

    返回的 `value` 是**表单里该填的原文**（`%` 字段填数值，如 `8.5` 表示 8.5%）——
    与「能确定的就不要人打字」的既有约定一致，界面直接把它填进那个输入框。
    """
    mat = _CACHE.get(path)
    if mat is None:
        return {"ok": False, "error": "这份材料还没载入过 —— 先在第一步解析它"}
    if not text:
        return {"ok": False, "error": "说点什么（例如「增长率按 5%」）"}

    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(%|％|个点)?", text)
    if not m:
        return {"ok": False,
                "error": "这句话里没有数字 —— 我只会把它转成字段值，不猜你的意思"}
    raw = m.group(1)
    is_pct = bool(m.group(2))

    low = text.lower()
    qs = intake.questions(mat)                 # **只调一次** —— 两次调用可能给出不同对象
    # 标签里的括号说明要去掉再比 —— 用户不会照着「（逐年，逗号分隔）」念。
    core = {id(q): re.sub(r"[（(].*?[）)]", "", getattr(q, "label", "") or "").strip()
            for q in qs}
    cands: list[tuple[str, str]] = []          # (key, label)
    for q in qs:
        key = getattr(q, "key", "")
        label = getattr(q, "label", "") or key
        if not key:
            continue
        c = core.get(id(q), "")
        if (label and label in text) or (c and c in text) or key in low:
            cands.append((key, label))
    if not cands:
        for word, frag in _ASK_ALIASES:
            if word in low or word in text:
                for q in qs:
                    label = getattr(q, "label", "") or ""
                    if frag in label or frag.lower() in label.lower():
                        cands.append((getattr(q, "key", ""), label))
            if cands:
                break
    # 去重（同一键只留一条）
    seen: set[str] = set()
    uniq = [(k, l) for k, l in cands if not (k in seen or seen.add(k))]
    if not uniq:
        return {"ok": False,
                "error": "认不出这是哪一项 —— 换一种说法（用问题清单里的名字，"
                         "例如「永续增长率」「折旧摊销占收入比」）"}
    if len(uniq) > 1:
        return {"ok": False, "ambiguous": True,
                "candidates": [{"key": k, "label": l} for k, l in uniq[:6]],
                "error": f"这句话可能指 {len(uniq)} 项 —— 请点一个："
                         + "、".join(l for _, l in uniq[:6])}
    key, label = uniq[0]
    value = raw + ("%" if is_pct else "")
    return {"ok": True, "proposal": {
        "key": key, "label": label, "value": value,
        "source": "对话输入（未核实）", "confidence": "低",
        "why": f"按「{m.group(0).strip()}」匹配到问题清单里的「{label}」",
    }}


def api_scan(path: str, unit: str = "") -> dict:
    """第 1、2 步：认材料 + 提取核对。"""
    mat = intake.scan(path, unit=unit)
    _CACHE[str(path)] = mat
    return material_json(mat)


def material_json(mat: Materials) -> dict:
    tables = []
    for kind in ("balance", "income", "cash_flow"):
        name = mat.detected.get(kind)
        c = next((x for x in mat.candidates
                  if x.kind == kind and name and x.path.name == name), None)
        tables.append({
            "kind": kind,
            "label": intake.LABEL[kind],
            "file": name or "",
            "mapped": c.mapped if c else 0,
            "rows": c.rows if c else 0,
            "rate": round(c.rate * 100, 1) if c else 0.0,
            "found": c is not None,
        })

    checks = []
    if mat.statements is not None:
        for c in mat.statements.checks():
            checks.append({
                "name": getattr(c, "name", "") or getattr(c, "title", "") or "勾稽",
                "ok": c.ok,
                "applicable": bool(c.applicable),
            })

    s = mat.statements
    return {
        "ok": mat.statements is not None,
        "path": str(mat.directory),
        "label": mat.label,
        "is_file": mat.is_file,
        "source": mat.source,
        "tables": tables,
        "unit": mat.unit,
        "unit_basis": mat.unit_basis,
        "gaap": getattr(s, "gaap", "") if s else "",
        "scope": getattr(s, "scope", "") if s else "",
        "audited": getattr(s, "audited", "") if s else "",
        "period": getattr(s, "period", "") if s else "",
        "checks": checks,
        "unused": mat.unused,
        "notes": mat.notes,
        "warnings": list(getattr(s, "warnings", []) or []) if s else [],
        # 映射率过低 → 界面上要顶一条红字（"表没读懂" ≠ "报表里没有"）
        "mapping_warnings": (s.mapping_warnings() if s is not None else []),
        "questions": [q.__dict__ for q in intake.questions(mat)],
        "out_dir": str(intake.out_dir_for(mat)),
    }


def api_appraise(path: str, unit: str, answers: dict[str, str],
                 out_dir: str | None = None) -> dict:
    """第 5、6 步：把表单里的回答变成配置，出报告。"""
    key = str(path)
    mat = _CACHE.get(key) or intake.scan(path, unit=unit)
    _CACHE[key] = mat

    ans: dict[str, Answer] = {}
    for k, v in (answers or {}).items():
        v = (v or "").strip()
        if v:
            ans[k] = parse_answer(v)
    if unit and not ans.get("unit"):
        ans["unit"] = parse_answer(unit)

    # 百分比字段：界面上把 % 显示在框里，人只填数值（填 8.5 即 8.5%）。
    # **在引擎拿到之前把 % 补进字符串** —— 值自带单位，下游就不用猜；
    # 猜错的代价是 100 倍级的静默错误（8.5 vs 850%）。只走网页这条路。
    intake.normalize_percents(mat, ans)

    if not (ans.get("unit") or mat.unit):
        return {"ok": False, "error": "单位还没确定（报表可能是千元/千美元，"
                                      "引擎默认万元 —— 差 1000 倍）。"
                                      "在上面的「金额单位」里填一个。"}

    # 口径三项：页面上能覆盖材料推出来的值（机器只推，改由人定）
    for attr in ("gaap", "scope", "audited"):
        if ans.get(attr):
            setattr(mat.statements, attr, ans[attr].value.strip())

    cfg, missing = intake.build_config(mat, ans)
    from value import run_report

    out = intake.out_dir_for(mat, out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = intake._slug(cfg.get("target") or "估值")
    cfg_path = out / f"{stem}.config.json"
    report_path = out / f"{stem}.报告.txt"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    report = run_report(cfg, cfg_path.parent, statements=mat.statements)
    report_path.write_text(report, encoding="utf-8")
    note_appraise()          # 记一笔"用过"—— 引导据此不再打扰（见 usage_state）

    return {
        "ok": True,
        "report": report,
        "missing": list(dict.fromkeys(missing)),
        "files": {"config": str(cfg_path), "report": str(report_path)},
        "out_dir": str(out),
    }


# ─────────────────── 模型配置页（本机 / 云端 / 用途 / 审计） ───────────────────

def api_audit(n: int = 20) -> list[dict]:
    """出网审计的尾巴 —— **让"发了什么"看得见**。

    审计里记的是 主机 / 用途 / 字节数 / sha256 / 谁授权 / 发了什么的描述，
    **不记内容本身**（否则审计文件自己会变成第二个泄密点）。
    """
    import guard
    p = Path(guard.AUDIT_PATH)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").strip().splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def api_config() -> dict:
    """配置页要的全部数据。**密钥只给打码值。**"""
    from llm import backends as lb
    from llm import cloud
    from llm import config as lcfg

    cfg = lcfg.load()
    runtimes: list[dict] = []

    ob = lb.OllamaBackend(base_url=cfg["local"]["ollama_url"])
    ok, why = ob.available()
    models: list[str] = []
    if ok:
        try:
            models = ob.list_models()
        except Exception as exc:                        # noqa: BLE001
            why = f"{why}；列模型失败：{exc}"
    runtimes.append({"name": "ollama", "label": "Ollama",
                     "url": cfg["local"]["ollama_url"], "ok": ok, "why": why,
                     "models": models})

    if cfg["local"].get("openai_compat_enabled"):
        cb = lb.OpenAICompatBackend(base_url=cfg["local"]["openai_compat_url"])
        ok2, why2 = cb.available()
        ms: list[str] = []
        if ok2:
            try:
                ms = cb.list_models()
            except Exception as exc:                    # noqa: BLE001
                why2 = f"{why2}；列模型失败：{exc}"
        runtimes.append({"name": "openai-compat", "label": "OpenAI 兼容（LM Studio / vLLM / llama.cpp）",
                         "url": cfg["local"]["openai_compat_url"], "ok": ok2,
                         "why": why2, "models": ms})

    providers = []
    for name, info in cloud.PROVIDERS.items():
        c = cfg["cloud"].get(name, {})
        key = lcfg.key_for(cfg, name)
        providers.append({
            "name": name, "label": info["label"], "kind": info["kind"],
            "host": info["host"], "models": list(info["models"]),
            "note": info.get("note", ""),
            "enabled": bool(c.get("enabled")),
            "key_display": cloud.mask(key), "has_key": bool(key),
            "key_file": (c.get("api_key_file") or info.get("key_file") or ""),
        })

    return {"ok": True, "runtimes": runtimes, "providers": providers,
            "purposes": cfg["purposes"], "purpose_label": lcfg.PURPOSE_LABEL,
            "local": cfg["local"],
            "config_path": str(lcfg.DEFAULT_PATH), "audit": api_audit(8),
            # 指引（哪台机器配哪个模型）—— **单一来源在 llm/guide.py**，
            # 页面只渲染，不自己写一份建议：两处各写一套，早晚对不上。
            "guide": guide_payload()}


def guide_payload() -> dict:
    """给页面/说明文件用的指引数据：分档建议 + 本机实测 + 怎么选 + 没有模型怎么办。"""
    from llm import guide

    d = guide.load_measured()
    ram = guide.detect_ram_gb() or (d.get("machine") or {}).get("ram_gb") or 0
    return {"tiers": [dict(t) for t in guide.NO_GPU_TIERS],
            "measured": guide.measured_rows(),
            "machine": (d.get("machine") or {}),
            "howto": list(guide.HOWTO),
            "speed_tiers": [{"min": a, "name": b, "note": c}
                            for a, b, c in guide.SPEED_TIERS],
            # 还没有本地模型的人：三条路 + 照做三步 + 按内存推荐的 pull 命令
            "ram_gb": ram,
            "recommend": guide.recommend(ram),
            "no_model_paths": [dict(p) for p in guide.NO_MODEL_PATHS],
            "install_steps": list(guide.INSTALL_STEPS),
            # 质量门槛实测（本地 vs 云端）—— **指引里最该说清的一句话**
            "quality": guide.quality_rows(),
            "truth": guide.truth_lines()}


def api_config_save(body: dict) -> dict:
    """只认这三种字段（本机地址 / 云端开关与密钥 / 用途绑定），别的一律忽略。"""
    from llm import cloud
    from llm import config as lcfg

    cfg = lcfg.load()
    local = body.get("local") or {}
    for k in ("ollama_url", "openai_compat_url"):
        if local.get(k):
            cfg["local"][k] = str(local[k]).strip()
    if "openai_compat_enabled" in local:
        cfg["local"]["openai_compat_enabled"] = bool(local["openai_compat_enabled"])

    for name, c in (body.get("cloud") or {}).items():
        if name not in cloud.PROVIDERS:
            continue
        tgt = cfg["cloud"].setdefault(name, {"enabled": False, "api_key": "",
                                             "api_key_file": ""})
        if "enabled" in c:
            tgt["enabled"] = bool(c["enabled"])
        if c.get("api_key_file") is not None:
            tgt["api_key_file"] = str(c.get("api_key_file") or "").strip()
        # 密钥：空字符串 = **不改**（避免"界面拿不到原文 → 一保存就把密钥清空"）
        if str(c.get("api_key") or "").strip():
            tgt["api_key"] = str(c["api_key"]).strip()

    for purpose, model in (body.get("purposes") or {}).items():
        if purpose in lcfg.PURPOSE_LABEL:
            cfg["purposes"][purpose] = str(model).strip()

    p = lcfg.save(cfg)
    return {"ok": True, "path": str(p), "config": lcfg.masked(lcfg.load())}


def api_gate(model: str, runs: int = 1) -> dict:
    """跑一次模型质量门槛。**这是"能用不能用"的裁决，不是跑分。**"""
    from llm import gate as lg

    if not model.strip():
        return {"ok": False, "error": "先选一个模型"}
    try:
        r = lg.run_gate(model.strip(), timeout=900)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "model": r.model, "passed": r.passed, "score": r.score,
            "failed": list(r.failed), "error": r.error, "report": r.report,
            "line": r.line(), "detail": lg.detail_lines(r)}


def api_cloud_check(provider: str, model: str, what: str) -> dict:
    """云端**试一次** —— 这一次调用要按次授权，授权也写进审计。

    发出去的只有一句合成测试话术（**不含任何材料内容**），
    `what` 是人在界面上确认过的"这次到底发什么"。
    """
    from guard import CloudConsent
    from llm import cloud
    from llm import config as lcfg

    info = cloud.PROVIDERS.get(provider)
    if not info:
        return {"ok": False, "error": f"不认识的服务商 {provider}"}
    if info["kind"] != "ready":
        return {"ok": False, "error": f"{info['label']} 需要单独适配（{info.get('note','')}）"}

    cfg = lcfg.load()
    if not (cfg["cloud"].get(provider, {}) or {}).get("enabled"):
        return {"ok": False, "error": f"{info['label']} 还没在配置里启用 —— "
                                      "启用之后才能试（云端默认关闭是刻意的）"}
    b = cloud.build(provider, api_key=lcfg.key_for(cfg, provider))
    ok, why = b.available()
    if not ok:
        return {"ok": False, "error": why}

    prompt = "请只回复两个字：可用"
    consent = CloudConsent(
        host=b.host, purpose=f"配置页试一次：{info['label']}",
        what=(what.strip() or "一句话测试（不含任何材料内容）"),
        approved_by="user:配置页确认")
    try:
        r = b.generate(model or b.models[0], prompt=prompt, system="",
                       timeout=60, consent=consent)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "reply": (r.text or "").strip()[:80],
            "host": b.host, "model": r.model,
            "sent_bytes": len(prompt.encode("utf-8"))}


# ─────────────────── 使用状态：引导该不该出现 ───────────────────
#
# **为什么记在服务端而不是浏览器**：浏览器把"看过"记在 localStorage 里是按**站点**
# 记的，而这个服务端口会变（8765 被占就往后换一个）—— 换了端口就是一个新站点，
# 引导会**再弹一次**；反过来清了缓存又会被拦一次教学。
# 所以"这台机器上用过没有"记在本地文件里，按安装记，不按浏览器记。

#: 使用状态文件。**可以用环境变量改路径** —— 测试必须能把它指到临时目录去，
#: 否则跑一次测试就把人真实的使用记录改了（真踩过：测试跑了 10 次估值，
#: 用户的 runs 直接变成 10）。
UI_STATE = Path.home() / ".freeanalyst" / "ui-state.json"


def _running_tests() -> bool:
    """在跑 unittest 吗 —— 跑测试时**不许写用户真实的使用记录**。

    为什么要有这个判断：测试里有好几处会真的跑 `api_appraise`，而它会记一笔
    "用过"。真踩过：跑一次测试，用户的 runs 从 0 变成 10，引导从此再也不弹。
    靠测试自己去设环境变量不可靠（`discover -s tests` 不会导入包级 `__init__`），
    所以这里主动让开。
    """
    import sys

    return "unittest" in sys.modules and "unittest" in " ".join(sys.argv[:2])


def _ui_state_path() -> Path:
    import os

    env = os.environ.get("FREANALYST_UI_STATE")
    if env:
        return Path(env)
    if _running_tests():
        global _TEST_STATE
        if _TEST_STATE is None:
            import tempfile

            _TEST_STATE = (Path(tempfile.mkdtemp(prefix="freeanalyst-test-"))
                           / "ui-state.json")
        return _TEST_STATE
    return UI_STATE


_TEST_STATE: Path | None = None


def _load_ui_state() -> dict:
    try:
        return json.loads(_ui_state_path().read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def _save_ui_state(d: dict) -> None:
    p = _ui_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:                                       # noqa: BLE001
        pass                                                # 记不住不是错误，别打断人


def usage_state() -> dict:
    """这台机器上用过没有 —— 页面据此决定要不要弹引导。"""
    d = _load_ui_state()
    return {"onboarded": bool(d.get("onboarded_at")),
            "runs": int(d.get("runs") or 0),
            "first_run": d.get("first_run") or ""}


def note_appraise() -> None:
    """成功出了一次估值 —— **这是"用过"的最强信号**（比"看过引导"强）。"""
    d = _load_ui_state()
    d["runs"] = int(d.get("runs") or 0) + 1
    d.setdefault("first_run", time.strftime("%Y-%m-%d %H:%M"))
    _save_ui_state(d)


def api_onboarded() -> dict:
    """页面点完引导后回记一笔（服务端 + 浏览器各记一次，互为兜底）。"""
    d = _load_ui_state()
    d.setdefault("onboarded_at", time.strftime("%Y-%m-%d %H:%M"))
    _save_ui_state(d)
    return {"ok": True, **usage_state()}


# ─────────────────── 装模型：服务端拉取，不用开终端 ───────────────────
#
# 为什么放在服务端做：让**本机的 ollama**自己去下载，界面只读进度。
# 用户点一下按钮就行 —— 不用开终端、不用记 `ollama pull` 怎么写。
# 拉的是回环地址（127.0.0.1:11434），不是外部主机。

_PULLS: dict[str, dict] = {}
_PULL_LOCK = threading.Lock()


def api_pull_start(model: str) -> dict:
    """开始拉一个模型（后台线程），立刻返回 —— 界面轮询进度。"""
    from llm import registry

    model = (model or "").strip()
    if not model:
        return {"ok": False, "error": "先填一个模型名，例如 qwen3.5:9b"}
    # **按形状校验，不是只列字符白名单。** 只列白名单时会漏掉 `../../etc/passwd`
    # 这种"字符合法但形状荒唐"的名字（实测被测试抓出来过）。
    import re as _re

    if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(:[A-Za-z0-9._-]+)?", model) \
            or ".." in model or len(model) > 80:
        return {"ok": False,
                "error": "模型名形状不对 —— 应该是 `家族:档位`（如 qwen3.5:9b）："
                         "以字母数字开头，只含字母数字与 . _ - ，不含 `..`"}

    with _PULL_LOCK:
        cur = _PULLS.get("current") or {}
        if cur.get("running"):
            return {"ok": False, "error": f"已经在拉 {cur.get('model')}，等它跑完"}
        _PULLS["current"] = {"model": model, "running": True, "status": "开始…",
                             "completed": 0, "total": 0, "error": "", "ok": None}

    def _run() -> None:
        def on_progress(status: str, done: int, total: int) -> None:
            with _PULL_LOCK:
                _PULLS["current"].update({"status": status, "completed": done,
                                          "total": total})

        r = registry.pull_via_ollama(model, on_progress=on_progress)
        with _PULL_LOCK:
            _PULLS["current"].update({"running": False, "ok": bool(r.get("ok")),
                                      "error": r.get("error", ""),
                                      "status": r.get("status", "")})

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "model": model}


def api_pull_status() -> dict:
    with _PULL_LOCK:
        return dict(_PULLS.get("current") or {"model": "", "running": False})


def api_model_check(ram_gb: float = 0, consent_ok: bool = False) -> dict:
    """查"当前该装哪个"。

    `consent_ok` = 用户在界面上确认过这次联网（界面会把 **主机 / 用途 / 这次发什么**
    原样写出来）。没有它就不联网，回退本地清单并**说明是回退**。
    """
    from guard import CloudConsent
    from llm import registry

    consent = None
    if consent_ok:
        consent = CloudConsent(
            host=registry.HOST,
            purpose="查模型体积与当前代次（只发模型名，不含材料）",
            what="模型家族名（如 qwen3.5）—— 不含任何材料内容、公司名、文件名",
            approved_by="user:配置页确认")
    return {"ok": True, **registry.check_candidates(ram_gb, consent=consent,
                                                    refresh=bool(consent_ok))}


# ─────────────────────────── HTTP 层 ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "FreeAnalyst"

    def log_message(self, format, *args):              # noqa: A002 - 与基类同签名
        """别把每次请求都打到终端 —— 这里不是给日志用的服务。"""
        pass

    # ---------- 工具 ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # **本机工具，任何东西都不缓存。**
        # 实测踩到：页面明明改好了，浏览器却给旧的那份 —— 表现是"新加的 tab 一直是灰的、
        # 新卡片不出现"，而服务端一切正常（像是代码没生效）。本地工具缓存自己的页面
        # **只有坏处**：省不了多少，却会让"改了没效果"变成常态。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # 本地工具，**不许任何外部页面把它嵌进 iframe**（防点击劫持）
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------- 路由 ----------
    def do_GET(self):                                  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, page_html().encode(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._json({"ok": True, "version": VERSION,
                        "endpoints": list(ENDPOINTS), "pages": list(PAGES),
                        "features": list(FEATURES),
                        # 用过没有 —— 页面据此决定要不要弹引导（见 usage_state）
                        "usage": usage_state()})
        elif self.path in ("/doc", "/doc.html"):
            self._send(200, doc_html().encode(), "text/html; charset=utf-8")
        elif self.path in ("/config", "/config.html"):
            self._send(200, config_html().encode(), "text/html; charset=utf-8")
        elif self.path == "/api/config":
            self._json(api_config())
        elif self.path == "/api/audit":
            self._json({"ok": True, "lines": api_audit(20)})
        elif self.path == "/api/pull-status":
            self._json(api_pull_status())
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):                                 # noqa: N802
        body = self._read_json()
        try:
            if self.path == "/api/scan":
                p = (body.get("path") or "").strip()
                if not p:
                    self._json({"ok": False, "error": "先给材料路径"}, 400)
                    return
                if not Path(p).expanduser().exists():
                    self._json({"ok": False, "error": f"这个路径不存在：{p}"}, 400)
                    return
                self._json(api_scan(p, body.get("unit") or ""))
            elif self.path == "/api/pick":
                # 弹系统原生选择框，把真实路径回给页面（不复制任何文件）
                self._json(pick_path(body.get("kind") or "dir"))
            elif self.path == "/api/appraise":
                self._json(api_appraise(body.get("path") or "",
                                        body.get("unit") or "",
                                        body.get("answers") or {}))
            elif self.path == "/api/config":
                self._json(api_config_save(body or {}))
            elif self.path == "/api/gate":
                self._json(api_gate(body.get("model") or "", body.get("runs") or 1))
            elif self.path == "/api/cloud-check":
                self._json(api_cloud_check(body.get("provider") or "",
                                           body.get("model") or "",
                                           body.get("what") or ""))
            elif self.path == "/api/pull":
                self._json(api_pull_start(body.get("model") or ""))
            elif self.path == "/api/model-check":
                self._json(api_model_check(float(body.get("ram_gb") or 0),
                                           bool(body.get("consent_ok"))))
            elif self.path == "/api/onboarded":
                self._json(api_onboarded())
            elif self.path == "/api/comps":
                self._json(api_comps(body or {}))
            elif self.path == "/api/ask":
                self._json(api_ask(body or {}))
            else:
                self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:                       # noqa: BLE001
            # **不许静默吞掉** —— 界面要看见失败，否则人以为在跑。
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


# ─────────────────────────── 页面 ───────────────────────────
# 六步向导：这一版做到 1、2、5、6（3、4 照实标注未接）。

PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>FreeAnalyst · 本地估值向导</title>
<style>
  :root{ --fg:#101828; --mut:#667085; --line:#e4e7ec; --acc:#4743E8; --bg:#fff; }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif}
  .wrap{max-width:960px;margin:0 auto;padding:34px 26px 80px}
  h1{font-size:21px;margin:0 0 4px;letter-spacing:.01em}
  .sub{color:var(--mut);font-size:12.5px;margin-bottom:22px}
  .steps{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:24px}
  .step{border:1px solid var(--line);border-radius:20px;padding:5px 13px;font-size:12px;
        color:var(--mut);display:flex;gap:7px;align-items:center}
  .step .n{font-variant-numeric:tabular-nums;font-weight:650}
  .step.on{border-color:var(--acc);color:var(--acc);font-weight:600}
  .step.off{opacity:.45;text-decoration:line-through}
  .card{border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px;
        transition:border-color .18s ease}
  .card:hover{border-color:var(--acc)}
  .card h2{font-size:14px;margin:0 0 10px}
  .card h2 span{font-weight:400;color:var(--mut);font-size:12px;margin-left:8px}
  input[type=text]{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
        font-size:13px;font-family:inherit;color:var(--fg);background:transparent}
  input[type=text]:focus{outline:none;border-color:var(--acc)}
  button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:9px 18px;
        font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
  button:disabled{opacity:.45;cursor:default}
  button.ghost{background:transparent;color:var(--acc);border:1px solid var(--acc)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line)}
  th{color:var(--mut);font-weight:600;font-size:11.5px}
  .ok{color:var(--acc);font-weight:600}
  .bad{color:#c0392b;font-weight:600}
  .mut{color:var(--mut)}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .q{display:grid;grid-template-columns:230px 1fr;gap:10px;align-items:start;
     padding:8px 0;border-bottom:1px solid var(--line)}
  .q label{font-size:12.5px}
  .q .hint{display:block;color:var(--mut);font-size:11.5px;margin-top:2px}
  .q input{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  pre{background:#f7f8fa;border:1px solid var(--line);border-radius:10px;padding:14px;
      overflow:auto;max-height:520px;font-size:11.5px;line-height:1.55;white-space:pre-wrap}
  .banner{border:1px dashed var(--line);border-radius:10px;padding:11px 14px;
          color:var(--mut);font-size:12.5px;margin-bottom:16px}
  .banner b{color:var(--fg)}
  .hide{display:none}
</style></head><body><div class="wrap">

  <h1>FreeAnalyst · 本地估值向导</h1>
  <div class="sub">本地跑的一个网页，全程不出网。每一步都是「程序准备好、你点头才往下走」。<br>
    <b>计算一个字都没改</b> —— 底下跑的就是命令行那条路（<code>intake.scan</code> → <code>value.run_report</code>）。</div>

  <div class="steps">
    <div class="step on" id="s1"><span class="n">1</span>丢材料</div>
    <div class="step" id="s2"><span class="n">2</span>提取核对</div>
    <div class="step off" id="s3"><span class="n">3</span>行业建议（未接）</div>
    <div class="step off" id="s4"><span class="n">4</span>可比公司（未接）</div>
    <div class="step" id="s5"><span class="n">5</span>假设清单</div>
    <div class="step" id="s6"><span class="n">6</span>结论</div>
  </div>

  <div class="banner">
    <b>这一版做到第 1、2、5、6 步。</b>第 3、4 步（行业建议、可比公司选取）需要先把流程层接进来，
    还没做 —— 画上去会变成"看起来能用"。可比公司代码可以在第 5 步的「可选」里填，那一条是通的。
  </div>

  <div class="card">
    <h2>1 · 丢材料<span>一个目录，或者单独一份 PDF</span></h2>
    <div class="row">
      <input type="text" id="path" placeholder="/Users/you/deals/某个标的/ 或 某公司2024年审计报告.pdf">
      <div><button id="scanBtn" onclick="scan()">认材料</button></div>
    </div>
    <div id="scanMsg" class="mut" style="margin-top:9px"></div>
  </div>

  <div class="card hide" id="card2">
    <h2>2 · 提取核对<span>三张表认出来没有、勾稽平不平、单位与口径是什么</span></h2>
    <div id="tables"></div>
    <div id="checks" style="margin-top:12px"></div>
    <div id="meta" style="margin-top:12px"></div>
    <div id="unitBox" style="margin-top:12px"></div>
  </div>

  <div class="card hide" id="card5">
    <h2>5 · 假设清单<span>必须由你给的数 —— 引擎不替你决定</span></h2>
    <div class="banner" style="margin:0 0 12px">
      每个数后面可以跟来源：<code>0.10 @管理层规划 p.12</code>；来源前写 <code>高:</code> / <code>中:</code> / <code>低:</code>
      可指定置信度。<b>不写来源 = 低置信度</b>，会出现在报告的"结果的软肋"里。
      注释里的「参考」是历史值，**不是建议值**。
    </div>
    <div id="questions"></div>
    <div style="margin-top:16px"><button id="goBtn" onclick="go()">出报告</button></div>
  </div>

  <div class="card hide" id="card6">
    <h2>6 · 结论<span id="outDir" class="mut"></span></h2>
    <div id="missing" style="margin-bottom:12px"></div>
    <pre id="report"></pre>
  </div>

<script>
const $ = id => document.getElementById(id);
let STATE = { path:"", unit:"", data:null };

function step(id, cls){
  const el = $(id);
  el.className = "step" + (cls ? " " + cls : "");
}

async function post(url, body){
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                              body: JSON.stringify(body)});
  return await r.json();
}

async function scan(){
  const p = $("path").value.trim();
  if(!p){ $("scanMsg").textContent = "先填一个路径"; return; }
  $("scanBtn").disabled = true;
  $("scanMsg").textContent = "在认材料…（PDF 走 OCR 可能要几分钟，别关页面）";
  const d = await post("/api/scan", {path:p, unit:STATE.unit});
  $("scanBtn").disabled = false;
  if(!d.ok){
    $("scanMsg").innerHTML = '<span class="bad">' + (d.error || "没认出来") + '</span>';
    if(d.tables){ renderTables(d); renderMeta(d); }
    return;
  }
  STATE.path = d.path; STATE.data = d;
  $("scanMsg").innerHTML = '<span class="ok">认出来了</span>';
  renderTables(d); renderChecks(d); renderMeta(d); renderUnit(d); renderQuestions(d);
  step("s2","on"); step("s5","on"); step("s6","on");
  $("card2").classList.remove("hide"); $("card5").classList.remove("hide");
  $("card2").scrollIntoView({behavior:"smooth"});
}

function renderTables(d){
  let h = '<table><tr><th>表</th><th>来源</th><th>映射</th><th>状态</th></tr>';
  for(const t of d.tables){
    h += '<tr><td>'+t.label+'</td><td class="mut">'+(t.file||"—")+'</td>'+
         '<td class="mut">'+(t.rows? t.mapped+"/"+t.rows+" 行（"+t.rate+"%）":"—")+'</td>'+
         '<td>'+(t.found?'<span class="ok">认出来了</span>':'<span class="bad">没认出来</span>')+'</td></tr>';
  }
  h += '</table>';
  if(d.unused && d.unused.length){
    h += '<div class="mut" style="margin-top:10px;font-size:12px"><b>没用上的文件（不是静默跳过）</b><br>'+
         d.unused.map(u=>"· "+u.replace(/</g,"&lt;")).join("<br>")+'</div>';
  }
  $("tables").innerHTML = h;
}

function renderChecks(d){
  let h = '<table><tr><th>勾稽校验</th><th>结果</th></tr>';
  for(const c of d.checks){
    let v = c.ok===true ? '<span class="ok">平</span>'
          : c.ok===false ? '<span class="bad">不平 —— 推算结果别用</span>'
          : c.applicable ? '<span class="mut">判不了（缺科目）</span>'
          : '<span class="mut">不适用（这种格式没这条）</span>';
    h += '<tr><td>'+c.name+'</td><td>'+v+'</td></tr>';
  }
  const bad = d.checks.some(c=>c.ok===false);
  if(bad){
    const dups = (d.warnings||[]).filter(w=>w.includes("个取值")).length;
    if(dups) h += '<tr><td colspan="2" class="mut">这份材料有 '+dups+
      ' 个科目出现多个取值 —— 很可能是同一页上既有合并表又有母公司表，取值取串了。'+
      '先确认取的是合并那一列。</td></tr>';
  }
  $("checks").innerHTML = h + '</table>';
}

function renderMeta(d){
  $("meta").innerHTML = '<div class="mut" style="font-size:12.5px">口径：'+
    '<b>'+(d.gaap||"未判定")+'</b> · <b>'+(d.scope||"未判定")+'</b> · <b>'+(d.audited||"未标注")+
    '</b> · 期间 <b>'+(d.period||"未标")+'</b>（不对就在下面改）</div>' +
    (d.notes && d.notes.length ? '<div class="mut" style="margin-top:8px;font-size:12px">'+
      d.notes.map(n=>"· "+n).join("<br>")+'</div>' : '');
}

function renderUnit(d){
  $("unitBox").innerHTML =
    '<div class="row"><div><label style="font-size:12.5px">金额单位（<b>错 1000 倍就是这里错</b>）</label>'+
    '<input type="text" id="unit" value="'+(d.unit||"")+'" placeholder="元 / 千元 / 万元 / 千美元"></div>'+
    '<div class="mut" style="align-self:end;font-size:12px">'+
    (d.unit ? "依据："+(d.unit_basis||"") : "<span class='bad'>没认出来 —— 必须你声明</span>")+
    '</div></div>';
}

function renderQuestions(d){
  let g = "";
  let h = "";
  for(const q of d.questions){
    if(q.group !== g){ g = q.group; h += '<h3 style="font-size:12.5px;margin:16px 0 4px;color:var(--acc)">'+g+'</h3>'; }
    const ref = q.reference ? "　｜ 参考："+q.reference : "";
    h += '<div class="q"><label>'+q.label+'<span class="hint">'+(q.hint||"")+ref+'</span></label>'+
         '<input type="text" data-key="'+q.key+'" value="'+(q.default||"").replace(/"/g,"&quot;")+'"></div>';
  }
  $("questions").innerHTML = h;
}

async function go(){
  $("goBtn").disabled = true;
  const answers = {};
  document.querySelectorAll("#questions input").forEach(i=>{ if(i.value.trim()) answers[i.dataset.key]=i.value.trim(); });
  const u = $("unit") ? $("unit").value.trim() : "";
  const d = await post("/api/appraise", {path:STATE.path, unit:u, answers:answers});
  $("goBtn").disabled = false;
  if(!d.ok){ $("missing").innerHTML = '<span class="bad">'+(d.error||"出错了")+'</span>'; return; }
  $("outDir").textContent = "产物：" + d.out_dir;
  let m = "";
  if(d.missing.length){
    m = '<div class="banner" style="margin:0 0 12px"><b>这几个没给，对应的那一块就没跑</b>（不是没发生）<br>'+
        d.missing.map(x=>"· "+x).join("<br>")+'</div>';
  }
  $("missing").innerHTML = m;
  $("report").textContent = d.report;
  $("card6").classList.remove("hide");
  $("card6").scrollIntoView({behavior:"smooth"});
}
</script>
</div></body></html>
"""


def find_port(host: str, want: int, tries: int = 12) -> int:
    """端口被占就往后找一个。

    ## 为什么非要这个（实测踩到）

    第一次交付时，用户在浏览器里看到的是 `ERR_CONNECTION_REFUSED`：
    服务没起来，而页面上没有一行字告诉他为什么。
    **"打不开"绝不能是这种工具给人的第一印象** —— 它得自己说清楚是
    端口被占了、还是别的。

    所以：先试想要的那个，占了就往后挪，并把真实端口**打在屏幕上**、
    也用真实端口去开浏览器。宁可换端口，也不要一句 error。
    """
    for p in range(want, want + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise SystemExit(f"× {want} 起往后 {tries} 个端口都被占了 —— 用 --port 换一个")


def _ready_check(url: str) -> None:
    """起来之后自己请求一遍，成功就在屏幕上打一行。

    这一行的意思是「端口确实通了」—— 不用人靠浏览器去猜。
    失败也照实打出来（**不许静默**）。
    """
    try:
        with urllib.request.urlopen(url + "api/health", timeout=5) as r:
            ok = b'"ok": true' in r.read() or r.status == 200
        print(f"  ✓ 自检通过：{url} 能打开（这一行说明端口通了）" if ok
              else "  × 自检异常：服务起来了但健康检查没通过")
    except Exception as exc:                            # noqa: BLE001
        print(f"  × 自检失败：{type(exc).__name__}: {exc}")
        print("    如果你浏览器里看到「拒绝连接」，多半是**代理**把 127.0.0.1 也劫走了 ——")
        print("    在 Clash 的 bypass/直连列表里加上 127.0.0.1,localhost，或先关掉系统代理。")


def serve(materials: str | None = None, *, port: int = DEFAULT_PORT,
          open_browser: bool = True) -> int:
    """起本地服务。**只绑 127.0.0.1。**

    传了 `materials` 会预扫一遍并把它填进页面（省得手打路径）。
    """
    if materials:
        path = str(Path(materials).expanduser().resolve())
        print(f"预扫材料：{path}")
        try:
            _CACHE[path] = intake.scan(path)
        except Exception as exc:                        # noqa: BLE001
            print(f"  预扫失败（页面上可以重填）：{type(exc).__name__}: {exc}")

    real = find_port(HOST, port)
    if real != port:
        print(f"注意：{port} 被别的程序占了，改用 {real}")
    srv = ThreadingHTTPServer((HOST, real), Handler)
    url = f"http://{HOST}:{real}/"
    print("─" * 66)
    print(f"FreeAnalyst 本地向导　{url}")
    print(f"  只绑 {HOST} —— 同一个 WiFi 下的其他机器**访问不到**")
    print("  按 Ctrl-C 停止")
    print("─" * 66)
    threading.Timer(0.6, lambda: _ready_check(url)).start()
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停了。")
    finally:
        srv.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FreeAnalyst 本地网页向导")
    ap.add_argument("materials", nargs="?", default=None, help="材料目录或单个文件")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true", help="不自动开浏览器")
    args = ap.parse_args()
    return serve(args.materials, port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    sys.exit(main())
