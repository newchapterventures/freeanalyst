"""可比公司数据源 —— 默认走结构化，**搜索接口留可插拔**。

## 用户的原话（2026-09-17）

> 搜索，我同意用三（结构化数据源），但是要**保留一个接入搜索 API 的接口**，
> 用户可以定制接入其搜索能力。

## 为什么默认不用搜索

可比公司倍数最终要的是**结构化数字**：

    市值 = 收盘价 × 股本
    EV   = 市值 + 净债务 − 少数股东权益
    倍数 = EV / EBITDA

这些从交易所 / SEC 的**结构化接口**直接取，比让模型去读网页可靠得多 ——
网页上的市值是几天前的、EBITDA 口径各写各的。**搜索适合当起点，不适合当数据源。**

## 但仍然留搜索接口，因为有些情况结构化源覆盖不到

- 未上市公司（没有公开报价）：只能靠融资新闻、工商信息
- 非上市可比交易：并购公告里的对价和标的财务
- 小众市场：没有现成的结构化接口

这时用户接自己的搜索能力。**接口留在这里，但默认不启用。**

## 诚实原则（沿用 `datasources/__init__.py` 的风格）

**留了位置 ≠ 能用。** 每个源都要能回答 `available()`：
不能用的时候**说清缺什么**，不假装能跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class PeerRow:
    """一家可比公司的原始数据。**缺的字段留 None，不用 0 填。**"""

    name: str
    market: str = ""
    code: str = ""
    as_of: str = ""
    market_cap: float | None = None
    enterprise_value: float | None = None
    revenue: float | None = None
    ebitda: float | None = None
    ebit: float | None = None
    net_income: float | None = None
    equity: float | None = None
    currency: str = ""
    #: 财报期间（和 `as_of` 价格时点可能不同，要能分开说）
    fiscal_end: str = ""
    source: str = ""
    gaps: list[str] = field(default_factory=list)

    def missing_for(self, metric: str) -> list[str]:
        """算这个指标还缺什么。**缺就明说，不代填。**"""
        need = {
            "EV/EBITDA": ("enterprise_value", "ebitda"),
            "EV/EBIT": ("enterprise_value", "ebit"),
            "EV/Revenue": ("enterprise_value", "revenue"),
            "P/E": ("market_cap", "net_income"),
            "P/B": ("market_cap", "equity"),
            "EV/ARR": ("enterprise_value", "revenue"),
        }.get(metric, ())
        return [f for f in need if getattr(self, f, None) is None]


@runtime_checkable
class CompsSource(Protocol):
    """可比公司数据源的接口。**实现它就能插进来。**"""

    name: str

    def available(self) -> tuple[bool, str]:
        """能不能用。返回 `(可用?, 说明)`。

        **不能用的时候要在说明里写清缺什么** —— 是缺 key、缺依赖、
        还是这个市场本来就没覆盖。不要返回一个空列表假装查过了。
        """
        ...

    def peers(self, criteria) -> list[PeerRow]:
        """按筛选条件取可比公司。"""
        ...


@dataclass
class SearchAPI:
    """**搜索接口 —— 留给用户自己接。**

    结构化源覆盖不到的场景（未上市公司、非上市可比交易、小众市场）用它。

    ## 实现时必须遵守的

    **一、查询词由 `comps_workflow.build_queries()` 生成，不要自己拼。**
    那个函数会断言禁名表里的名字没混进查询 —— 自己拼就绕过了这道闸。

    **二、必须走 `guard.guarded_request()`，并且带 `CloudConsent`。**
    外部主机默认拦截。搜索在公网上跑，这一步不能省。

    **三、拿回来的是字符串，要自己转成 `PeerRow`。**
    这个接口不替你解析网页 —— 解析是各个搜索源自己的事。

    用法：

        from datasources import comps_source

        class MySearch:
            name = "my-search"
            def available(self):
                if not os.environ.get("MY_SEARCH_KEY"):
                    return False, "缺 MY_SEARCH_KEY"
                return True, ""
            def search(self, query, limit, consent):
                raw = guard.guarded_request(url, ..., consent=consent)
                return [...]                      # 自己解析

        comps_source.register(MySearch())
    """

    name: str = ""

    def available(self) -> tuple[bool, str]:
        return False, "搜索接口未实现 —— 需要用户接入自己的搜索能力"

    def search(self, query: str, limit: int, consent) -> list[dict]:
        raise NotImplementedError


#: 注册表 —— `name -> 源`
REGISTRY: dict[str, CompsSource] = {}


class SourceNotReady(RuntimeError):
    """数据源不可用时抛出。**带可操作的说明，不是一句「失败」。**"""


def register(src, *, replace: bool = False) -> None:
    """注册一个数据源。**同名默认拒绝覆盖** —— 静默替换会让人以为用的是自己的。"""
    name = getattr(src, "name", "") or type(src).__name__
    if name in REGISTRY and not replace:
        raise ValueError(
            f"数据源 `{name}` 已注册。要替换请显式 `replace=True` —— "
            f"静默替换会让人以为用的是自己接的那个。")
    src.name = name
    REGISTRY[name] = src


def get(name: str):
    return REGISTRY.get(name)


def list_sources() -> list[tuple[str, bool, str]]:
    """列出所有源和各自的可用状态。**给用户看，不是给程序判断。**"""
    out = []
    for name, src in sorted(REGISTRY.items()):
        try:
            ok, why = src.available()
        except Exception as e:                        # noqa: BLE001
            ok, why = False, f"检查时出错：{type(e).__name__}: {e}"
        out.append((name, ok, why))
    return out


def render_sources() -> str:
    if not REGISTRY:
        return ("  可比公司数据源：**一个都没注册**。\n"
                "    结构性数据源和搜索接口都要接 —— 见本模块文档。")
    lines = ["  可比公司数据源："]
    for name, ok, why in list_sources():
        lines.append(f"    {'✓' if ok else '✗'} {name:20} {why or '可用'}")
    return "\n".join(lines)
