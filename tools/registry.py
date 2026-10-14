"""工具注册表 —— 把「模型能调什么」变成一份**可审计的清单**。

## 这个模块在做什么

现在全项目的能力都是 **Python 函数**：`intake.scan`、`valuation.dcf`、
`datasources.prices`… 模型够不着它们。这一层给每个能力补三样东西：

    名字 + 一句话说明 + 参数的 JSON Schema

补完，模型才能「自己伸手」。**但伸手的方式要分级。**

## 分级是这一层的核心 —— 按**副作用**分三类

    read_local   纯读本机：不算数、不出网、不写盘     → 模型可自由调用
    egress       会访问外部网站                        → **必须过按次授权**
    write        写盘 / 改状态                         → 要用户点头

这个分级**不是新造一套闸门**，是给已有的三套闸门贴标签：

    egress  → `net.py` 的 `HostConsent`（按次授权，contextvar，出范围即失效）
    write   → 落盘前要确认
    read_local → 本来就安全

## ⚠️ `description` 里那句「什么时候**别用**」比「什么时候用」重要

实测（`bench/model_choice.py`）：模型**选错工具**的情况，
远少于**在不该用时伸手**。最典型的一条是——用户只打了个「你好」，
模型去扫了一个几百页的目录，然后白等两分钟。**下游看不出这个错。**

所以每个工具的说明都必须回答：

    什么时候用      用它干什么
    什么时候别用    ★ 什么情况下调它是错的

## 一条不做的事

**这一层不实现任何算术。** 工具只是把已有函数包起来 ——
`call()` 里没有一行计算逻辑。这条守的是项目那条老规矩：
**纯代码算，不用 LLM 算**；模型可以「决定调用什么」，不能「参与计算」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable


class SideEffect(str, Enum):
    """工具会碰到什么。**这是能不能让模型自由调用的唯一判据。**"""

    READ_LOCAL = "read_local"
    EGRESS = "egress"
    WRITE = "write"


#: 每一类的默认策略 —— 给人看的一句话。
POLICY: dict[SideEffect, str] = {
    SideEffect.READ_LOCAL: "模型可自由调用",
    SideEffect.EGRESS: "**必须过按次授权** —— 真去取的那一刻要用户点头",
    SideEffect.WRITE: "要用户点头后才落盘",
}


class ToolError(RuntimeError):
    """工具自己报的错（参数不对、材料没准备好）。**会让模型看到，让它自己改。**"""


@dataclass
class ToolResult:
    """一次调用的结果。

    **出错不抛异常，返回 `ok=False` + `error`。** 理由：抛出去模型看不到，
    它就没法根据错误换个动作 —— 而「换动作」正是编排能力本身。
    """

    ok: bool
    value: Any = None
    error: str = ""
    #: 这次调用**缺授权** —— 不是失败，是要先去问用户
    needs_consent: bool = False
    #: 需要授权的主机（`needs_consent` 为真时才有）
    hosts: list[str] = field(default_factory=list)

    @staticmethod
    def ok_(value: Any) -> "ToolResult":
        return ToolResult(ok=True, value=value)

    @staticmethod
    def fail(error: str) -> "ToolResult":
        return ToolResult(ok=False, error=error)

    def brief(self, limit: int = 400) -> str:
        """给模型看的一行摘要 —— **不要塞整个返回值进去**，token 会被吃光。"""
        if self.needs_consent:
            return f"需要用户授权才能访问：{'、'.join(self.hosts)}"
        if not self.ok:
            return f"出错了：{self.error}"
        try:
            t = json.dumps(self.value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            t = str(self.value)
        return t if len(t) <= limit else t[:limit] + f"…（共 {len(t)} 字）"


@dataclass
class Tool:
    name: str
    #: 一句话：这个工具干什么
    summary: str
    #: 什么时候用
    when: str
    #: ★ 什么时候别用 —— 比 `when` 重要
    when_not: str
    #: 参数的 JSON Schema（`{"type": "object", "properties": {...}}`）
    parameters: dict
    side_effect: SideEffect
    fn: Callable[..., Any]
    #: 给人看的成本说明（慢不慢、要不要等）
    cost: str = ""
    #: 出网类工具要访问的主机 —— 授权弹窗上要显示，所以必须显式列出来
    hosts: tuple[str, ...] = ()

    def description(self) -> str:
        """**给模型看的**那段话。三段都要有 —— 缺「什么时候别用」就会乱伸手。"""
        return (f"{self.summary}\n"
                f"什么时候用：{self.when}\n"
                f"什么时候别用：{self.when_not}")

    def schema(self) -> dict:
        """ollama / OpenAI 的 `tools=` 条目。"""
        return {"type": "function",
                "function": {"name": self.name,
                             "description": self.description(),
                             "parameters": self.parameters}}

    def validate(self, args: dict) -> str:
        """按 Schema 做**最小的**参数检查。返回错误说明，没问题返回空串。

        只查三件事：必填有没有给、有没有多给、类型对不对得上。
        **不做类型强转** —— 悄悄把 `"1e6"` 转成数字会让上游以为自己给对了。
        """
        if not isinstance(args, dict):
            return "参数必须是一个对象"
        props = self.parameters.get("properties") or {}
        required = self.parameters.get("required") or []
        for k in required:
            if k not in args or args[k] in (None, ""):
                return f"缺必填参数 {k!r}（{props.get(k, {}).get('description', '')}）"
        extra = [k for k in args if k not in props]
        if extra and self.parameters.get("additionalProperties") is False:
            return f"不认识的参数：{extra}"
        want = {"string": str, "number": (int, float), "integer": int,
                "boolean": bool, "array": list, "object": dict}
        for k, v in args.items():
            spec = props.get(k) or {}
            t = spec.get("type")
            # ⚠️ `bool` 要单独挡：Python 里 `True` **是 `int` 的子类**，
            # 光看 `isinstance` 会把它当整数收下 —— 于是 `{"n": true}` 静默变成 1。
            # 这正是本项目一直在防的那类错：**悄悄收下一个错的东西**。
            if t in ("number", "integer") and isinstance(v, bool):
                return f"参数 {k!r} 要是 {t}，收到 boolean"
            if t in want and v is not None and not isinstance(v, want[t]):
                return f"参数 {k!r} 要是 {t}，收到 {type(v).__name__}"
        return ""


# ─────────────────────────── 注册表 ───────────────────────────

REGISTRY: dict[str, Tool] = {}


def register(t: Tool, *, replace: bool = False) -> Tool:
    """注册一个工具。

    **同名默认拒绝覆盖** —— 静默替换会让人以为在用自己接的那个，
    实际被另一个顶掉了（`llm/backends.py` 里同一条规矩）。
    """
    if t.name in REGISTRY and not replace:
        raise ValueError(f"工具 {t.name!r} 已注册；要覆盖请显式 replace=True")
    if t.side_effect is SideEffect.EGRESS and not t.hosts:
        raise ValueError(
            f"出网工具 {t.name!r} 必须显式列出 hosts —— "
            f"授权弹窗要显示「去哪些网站取什么」，只给一个工具名没法让人做判断")
    REGISTRY[t.name] = t
    return t


def tool(name: str, *, summary: str, when: str, when_not: str,
         parameters: dict | None = None, side_effect: SideEffect = SideEffect.READ_LOCAL,
         cost: str = "", hosts: Iterable[str] = ()) -> Callable[[Callable], Tool]:
    """装饰器形式注册 —— 工具的实现就写在声明下面，不容易脱节。"""
    def wrap(fn: Callable) -> Tool:
        return register(Tool(name=name, summary=summary, when=when,
                             when_not=when_not, parameters=parameters or {"type": "object",
                                                                          "properties": {}},
                             side_effect=side_effect, fn=fn, cost=cost,
                             hosts=tuple(hosts)))
    return wrap


def get(name: str) -> Tool | None:
    return REGISTRY.get(name)


def by_side_effect(kind: SideEffect) -> list[Tool]:
    return sorted((t for t in REGISTRY.values() if t.side_effect is kind),
                  key=lambda t: t.name)


def schema(only: Iterable[str] | None = None) -> list[dict]:
    """给模型看的工具清单。`only` 可以只暴露一部分 —— **低配机器要收窄动作空间。**"""
    names = set(only) if only is not None else None
    return [t.schema() for t in sorted(REGISTRY.values(), key=lambda x: x.name)
            if names is None or t.name in names]


def call(name: str, args: dict | None = None, *,
         consent: dict[str, Any] | None = None) -> ToolResult:
    """调用一个工具。**参数不对就返回错误，不抛。**

    `consent`：出网工具要的按次授权（形如 `{"host": HostConsent}`）。
    没带 → 返回 `needs_consent=True`，由上层去问用户 —— **不当成失败**。
    """
    t = REGISTRY.get(name)
    if t is None:
        return ToolResult.fail(f"没有 {name!r} 这个工具")

    args = args or {}
    bad = t.validate(args)
    if bad:
        return ToolResult.fail(bad)

    # 出网：先看授权，再动手。**在调用之前挡**，不能先发出去再问。
    #
    # ⚠️ 只对**在按次授权名单里**的主机要授权。
    # 一律要的话，「取日线」这种日常数据源会天天弹窗 —— 而天天弹窗的结果
    # 是用户无脑点同意，那这道闸就等于没有。
    # 判据用 `net.host_needs_consent()`，**不是自己再判断一遍** ——
    # 名单只有一份，免得两边不一致。
    if t.side_effect is SideEffect.EGRESS:
        from net import host_needs_consent
        missing = [h for h in t.hosts
                   if host_needs_consent(h) and not (consent or {}).get(h)]
        if missing:
            return ToolResult(ok=False, needs_consent=True, hosts=missing,
                              error=f"缺授权：{'、'.join(missing)}")

    try:
        if t.side_effect is SideEffect.EGRESS and consent:
            from net import host_consent
            with host_consent(list(consent.values())):
                return ToolResult.ok_(t.fn(**args))
        return ToolResult.ok_(t.fn(**args))
    except Exception as exc:                     # noqa: BLE001
        # **把异常收成返回值** —— 抛出去模型看不到，它就没法换个动作再试
        return ToolResult.fail(f"{type(exc).__name__}: {exc}")


def render_catalog() -> str:
    """给**用户**看的清单 —— 按副作用分组，把每一条的策略写清楚。"""
    if not REGISTRY:
        return "  还没有注册任何工具。"
    out = []
    order = (SideEffect.READ_LOCAL, SideEffect.EGRESS, SideEffect.WRITE)
    title = {SideEffect.READ_LOCAL: "纯读本机", SideEffect.EGRESS: "会出网",
             SideEffect.WRITE: "会写盘"}
    for i, kind in enumerate(order, 1):
        rows = by_side_effect(kind)
        if not rows:
            continue
        out.append(f"  {'一二三'[i - 1]}、{title[kind]}　（{POLICY[kind]}）")
        for t in rows:
            out.append(f"    · {t.name}")
            out.append(f"        {t.summary}")
            if t.cost:
                out.append(f"        成本：{t.cost}")
            if t.hosts:
                out.append(f"        会访问：{'、'.join(t.hosts)}")
        out.append("")
    return "\n".join(out)
