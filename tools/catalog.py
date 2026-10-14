"""工具清单 —— 把已有能力包成模型能调的东西。

## 这一层的唯一职责：**包一层，不改原函数**

`call()` 路径里没有一行计算逻辑。工具只是把 `intake` / `valuation` /
`datasources` 里的函数接出来，补上名字、说明和参数 Schema。

理由：项目那条老规矩 —— **纯代码算，不用 LLM 算**。
模型可以「决定调用什么」，不能「参与计算」。包一层就不破坏它。

## 每个工具的说明都必须写「什么时候**别用**」

实测（`bench/model_choice.py`）：模型**选错工具**远少于**在不该用时伸手**。
最典型的一条 —— 用户只打了个「你好」，模型去扫了一个几百页的目录，
然后白等两分钟。**下游看不出这个错。**

## 出网的工具要显式列 `hosts`

授权弹窗上要显示「去哪些网站取什么」—— 只给一个工具名，用户没法做判断。

    fetch_price          日线源，**不在按次授权名单里**（日常数据源，不弹窗）
    fetch_share_count    股数源，**在按次授权名单里**（每取一次都要点头）
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import registry as R

if TYPE_CHECKING:                       # 只给类型检查看，运行时不用 import
    from intake import Materials

# ── 材料缓存 ──────────────────────────────────────────────────────────
#
# 一次会话里同一个目录只扫一遍。`intake.scan` 很贵（读 PDF、抽三张表、可能走 OCR），
# 而工具会被反复调用（「再看看缺什么」「现在算杜邦」）。
# 键是 (路径, 单位) —— **单位变了要重扫**，因为单位是取值口径的一部分。
_CACHE: dict[tuple[str, str], "Materials"] = {}


def _materials(path: str, unit: str = "") -> "Materials":
    """取（或扫出）一个材料目录。路径不对就抛 —— `call()` 会收成返回值。"""
    if not path:
        raise R.ToolError("没给材料路径（`materials`）")
    key = (str(path), unit or "")
    if key not in _CACHE:
        import intake
        p = Path(path).expanduser()
        if not p.exists():
            raise R.ToolError(f"路径不存在：{p}")
        _CACHE[key] = intake.scan(p, unit=unit)
    return _CACHE[key]


def clear_cache() -> None:
    """清缓存。测试用，也用于「我换了材料」这种场合。"""
    _CACHE.clear()


# ═══════════════════════════ 一、纯读本机 ═══════════════════════════

@R.tool(
    "scan_materials",
    summary="列出材料目录里有哪些文件。",
    when="刚拿到一个目录、还不知道里面有什么的时候。很快，不读内容。",
    when_not="目录已经扫过了 —— 重复列一遍是浪费；"
             "要看报表内容用 read_statements。",
    parameters={
        "type": "object",
        "properties": {
            "materials": {"type": "string", "description": "材料目录或单个文件的路径"},
        },
        "required": ["materials"],
    },
    cost="很快（只列文件名）",
)
def scan_materials(materials: str) -> dict:
    p = Path(materials).expanduser()
    if not p.exists():
        raise R.ToolError(f"路径不存在：{p}")
    if p.is_file():
        return {"kind": "file", "files": [p.name], "count": 1}
    files = sorted(str(x.relative_to(p)) for x in p.rglob("*") if x.is_file())
    return {"kind": "dir", "files": files, "count": len(files)}


@R.tool(
    "read_statements",
    summary="从材料里读出资产负债表、利润表、现金流量表。",
    when="材料里有年报/招股书/审计报告，而且还没读过。这是取数的第一步。",
    when_not="**已经读过而且材料没换** —— 很慢（读 PDF、可能走 OCR，"
             "上百页的要几十秒），而且结果会被缓存，重复调用是白等。",
    parameters={
        "type": "object",
        "properties": {
            "materials": {"type": "string", "description": "材料目录或单个文件的路径"},
            "unit": {"type": "string",
                     "description": "金额单位。材料里能认出来就留空；"
                                    "认不出来时必须由用户声明（认错会差百万倍）"},
        },
        "required": ["materials"],
    },
    cost="慢（约 8 秒/100 页；扫描件更久）",
)
def read_statements(materials: str, unit: str = "") -> dict:
    import intake
    mat = _materials(materials, unit)
    out = {"source": mat.source, "unit": mat.unit,
           "unit_basis": mat.unit_basis, "unit_missing": not mat.unit,
           "detected": dict(mat.detected), "ok": mat.ok()}
    facts = intake.cover_facts(mat)
    if facts:
        out["cover"] = facts
    s = mat.statements
    if s is not None:
        out["statements"] = {
            "gaap": s.gaap or "未判定", "scope": s.scope or "未判定",
            "audited": getattr(s, "audited", "") or "未标注",
            "period": s.period or "未标",
            # `s.balance` 是 `StatementSet`，字段在 `.fields` 里 —— 不是 dict
            "counts": {k: len(getattr(v, "fields", None) or {})
                       for k, v in (("balance", s.balance), ("income", s.income),
                                    ("cash_flow", s.cash_flow))},
        }
    return out


@R.tool(
    "list_gaps",
    summary="列出当前还缺哪些数据、以及每一项为什么算不出来。",
    when="要用某个数但手头没有，先看缺什么；或者用户要的东西算不出来。",
    when_not="已经知道缺什么了、或者根本不缺 —— 它只报告缺口，不会去补。",
    parameters={
        "type": "object",
        "properties": {
            "materials": {"type": "string", "description": "材料目录或单个文件的路径"},
        },
        "required": ["materials"],
    },
    cost="很快（材料已在缓存里）",
)
def list_gaps(materials: str) -> dict:
    mat = _materials(materials)
    gaps: list[str] = []
    if not mat.ok():
        gaps.append("三张表还没读出来 —— 先调 read_statements")
    if not mat.unit:
        gaps.append("**金额单位没认出来** —— 认错会差百万倍，必须由用户声明")
    for kind, name in (("balance", "资产负债表"), ("income", "利润表"),
                       ("cash_flow", "现金流量表")):
        if not mat.detected.get(kind):
            gaps.append(f"{name}没认出来")
    s = mat.statements
    if s is not None:
        try:
            gaps.extend(s.mapping_warnings() or [])
        except Exception:                                    # noqa: BLE001
            pass
        for w in (getattr(s, "warnings", None) or []):
            gaps.append(str(w))
        for c in (getattr(s, "conflicts", None) or []):
            gaps.append(str(c))
    for n in (mat.notes or []):
        gaps.append(str(n))
    return {"gap_count": len(gaps), "gaps": gaps[:40],
            "note": "这些是**取数层面**的缺口；假设层面的缺什么，看 assumption_questions。"}


@R.tool(
    "assumption_questions",
    summary="列出估值还需要用户定哪些假设（折现率、增速、利润率…）。",
    when="三张表读出来了，要往下做估值 —— 先看还缺哪些假设。",
    when_not="只做财务分析（杜邦那种）时不用 —— 那条路只读三张表，不碰假设。",
    parameters={
        "type": "object",
        "properties": {
            "materials": {"type": "string", "description": "材料目录或单个文件的路径"},
            "growth_years": {"type": "integer",
                             "description": "显式预测期年数，默认 5"},
        },
        "required": ["materials"],
    },
    cost="快",
)
def assumption_questions(materials: str, growth_years: int = 5) -> dict:
    import intake
    mat = _materials(materials)
    qs = intake.questions(mat, growth_years=growth_years)
    return {"count": len(qs),
            "questions": [{"key": q.key, "label": q.label,
                           "origin": getattr(q, "origin", ""),
                           "hint": (getattr(q, "hint", "") or "")[:200]}
                          for q in qs]}


@R.tool(
    "search_materials",
    summary="在材料原文里检索，返回带出处编号的片段。",
    when="要找某个说法、某个数字在材料哪一页；或者要核对一个结论有没有原文支撑。",
    when_not="要的是三张表里的数时不用 —— 那个用 read_statements，"
             "检索返回的是文字片段，不是结构化数字。",
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要查的问题"},
            "top_k": {"type": "integer", "description": "返回几段，默认 6"},
        },
        "required": ["question"],
    },
    cost="快（本地 BM25，不出网）",
)
def search_materials(question: str, top_k: int = 6) -> dict:
    from retrieval import BM25, Chunk
    import freeanalyst as fa
    chunks = fa.load_index()
    if not chunks:
        raise R.ToolError("本机索引是空的 —— 先 `freeanalyst.py ingest <材料目录>`")
    hits = BM25(chunks).search(question, top_k=top_k)
    return {"count": len(hits),
            "hits": [{"id": c.chunk_id, "source": str(c.source),
                      "text": (c.text or "")[:600]} for c, _ in hits]}


@R.tool(
    "dupont",
    summary="把 ROE 拆成三块（净利率 × 资产周转率 × 权益乘数）。",
    when="要走「财务分析」这条路 —— 它只读三张表，不碰假设。",
    when_not="缺三张表里的关键项时算不出来（会明说缺哪项，不拿 0 填）；"
             "要做估值不要用它，那是另一条路。",
    parameters={
        "type": "object",
        "properties": {
            "materials": {"type": "string", "description": "材料目录或单个文件的路径"},
        },
        "required": ["materials"],
    },
    cost="快",
)
def dupont(materials: str) -> dict:
    from dataclasses import asdict
    from valuation.dupont import analyse
    mat = _materials(materials)
    if mat.statements is None:
        raise R.ToolError("三张表还没读出来 —— 先调 read_statements")
    d = analyse(mat.statements)
    return asdict(d)


@R.tool(
    "metric_reference",
    summary="列出材料里能算出来的参考值（比率、增速这类）和各自的取数依据。",
    when="要摆出「这个数是从哪来的」；或者想看哪些比率材料本身就能支持。",
    when_not="**它不替你算估值** —— 只是把已有的比率列出来。要估值用 dcf / multiples。",
    parameters={
        "type": "object",
        "properties": {
            "materials": {"type": "string", "description": "材料目录或单个文件的路径"},
            "unit": {"type": "string", "description": "金额单位，一般留空"},
        },
        "required": ["materials"],
    },
    cost="快",
)
def metric_reference(materials: str, unit: str = "") -> str:
    import intake
    mat = _materials(materials, unit)
    return intake.metric_reference(mat, unit=unit or "")


# ═══════════════════════════ 二、会出网 ═══════════════════════════
#
# ⚠️ 这里的 `hosts` 用来决定**要不要按次授权**：
#     `net.CONSENT_HOSTS` 里的主机 → 每次都要用户点头
#     只是白名单里的主机         → 日常数据源，不弹窗

@R.tool(
    "fetch_price",
    summary="取某个股票代码在指定日期的收盘价（不复权）。",
    when="要算市值/倍数，而且**已经知道基准日**。",
    when_not="**没有基准日时不要用** —— 拿今天的股价配去年的资产负债表，"
             "是这类工具最容易犯又最看不出来的错（实测差 10%）。"
             "另外估值用**基准日收盘价**，不是最新价。",
    parameters={
        "type": "object",
        "properties": {
            "market": {"type": "string", "description": "市场：sh / sz / hk / us"},
            "code": {"type": "string", "description": "股票代码"},
            "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
        },
        "required": ["market", "code", "as_of"],
    },
    side_effect=R.SideEffect.EGRESS,
    hosts=("push2his.eastmoney.com",),
    cost="慢（要访问日线源，约 1–3 秒）",
)
def fetch_price(market: str, code: str, as_of: str) -> dict:
    from datasources.prices import close_on
    bar = close_on(market, code, as_of, adjust=0)
    if bar is None:
        raise R.ToolError(f"取不到 {market}{code} 在 {as_of} 或之前的收盘价")
    return {"market": market, "code": code, "as_of": as_of,
            "date": bar.date, "close": bar.close, "adjust": "不复权"}


@R.tool(
    "fetch_share_count",
    summary="取某个股票代码的总股本（算市值用：市值 = 基准日收盘价 × 总股本）。",
    when="要算市值或 EV 类倍数，而且手头没有股数（材料里也读不到）。",
    when_not="材料里已经有股本时不要用 —— 材料里的数是审计过的，比外部源可信。"
             "**这一步会访问需按次授权的主机，每次都要用户点头。**",
    parameters={
        "type": "object",
        "properties": {
            "market": {"type": "string", "description": "市场：sh / sz"},
            "code": {"type": "string", "description": "股票代码"},
        },
        "required": ["market", "code"],
    },
    side_effect=R.SideEffect.EGRESS,
    hosts=("qt.gtimg.cn", "push2.eastmoney.com"),
    cost="慢（要访问两个按次授权的主机）",
)
def fetch_share_count(market: str, code: str) -> dict:
    from datasources import cn_financials
    got = cn_financials.fetch(market, code) if hasattr(cn_financials, "fetch") else None
    if not got:
        raise R.ToolError(f"取不到 {market}{code} 的股本")
    return got


# ═══════════════════════════ 自检 ═══════════════════════════

def all_tools() -> list[dict]:
    """给模型用的工具清单（`tools=` 那个参数）。"""
    return R.schema()
