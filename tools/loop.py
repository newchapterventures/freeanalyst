"""编排循环 —— 让模型**一步一步**决定调什么工具，直到能给出结论。

## 用户定的自主度（原话）

> 模型可以给出选项和自动化操作路径，但是**由用户决定方向**，然后模型问什么、
> 取什么，也是用户决定，甚至可以通过跟用户的交互，得到去什么。
> **既可以一键得到答案，也可以由用户决定路径和各种细节**
> —— 取决于跟用户交互的结果。

所以这里有**一个滑块**，不是一种模式：

    one_click   模型跑完，只把结论和轨迹给你        ← 方向你给了，细节全交出去
    confirm     模型提议 → 你点头才执行              ← 默认
    manual      你说做什么它做什么                   ← 不经这个循环

**滑块的位置可以中途调** —— 因为「取决于跟用户交互的结果」。
每一步都带 `Autonomy`，所以轨迹里能看出哪几步是「授权自决」的。

## ★ 轨迹里必须分清「谁定的」

这是用户明确要求的：

> 轨迹里要分清「这个数是模型自决的」还是「你拍的」。
> **否则回头看会分不清哪些判断是你的。**

所以 `Step.by` 每一格都记：`model`（模型自决）/ `user`（用户点头）/ `auto`（授权自决）。
少了这个，事后回看只能看到「系统建议的」，看不出「你当时其实改了」。

## 循环里挡掉的三件事

1. **重复调用同一个工具同样的参数** —— 模型特别爱这么干。
   第二次直接返回「刚才已经调过了，结果一样」，**不真的再跑一遍**（扫描很贵）。
2. **超过步数上限** —— 停，并把「为什么没走完」写进轨迹。
3. **出网前暂停** —— 走 `ToolResult.needs_consent`，交给 `on_ask` 去问用户。
   **在调用之前挡**，不能先发出去再问。

## 这一层不做的事

**不做算术，也不替模型做计划。** 循环只负责：把工具清单给它、执行它选的、
把结果喂回去、记下来。它自己决定下一步 —— 那正是「agent」的定义。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from . import registry as R

#: ollama 原生接口。**不是 /v1** —— 那边关不掉思考，答案会全挤进 reasoning。
OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"

DEFAULT_MODEL = "qwen3.5:9b"
TIMEOUT = 300


class Autonomy(str, Enum):
    ONE_CLICK = "one_click"
    CONFIRM = "confirm"


#: 滑块每一档的人话说明 —— 界面和轨迹里都用它。
AUTONOMY_LABEL: dict[Autonomy, str] = {
    Autonomy.ONE_CLICK: "一键：模型一路走完，只把结论和轨迹给你",
    Autonomy.CONFIRM: "每步确认：模型提议，你点头才执行",
}


class By(str, Enum):
    """这一步**谁定的**。★ 用户明确要求要分清。"""

    MODEL = "model"       # 模型自己选的（一键模式）
    USER = "user"         # 用户当场点的头
    AUTO = "auto"         # 用户事先授权的范围之内


class Stop(str, Enum):
    DONE = "done"                 # 模型说做完了
    MAX_STEPS = "max_steps"       # 到步数上限
    REFUSED = "refused"           # 用户拒了某一步
    NEEDS_CONSENT = "needs_consent"   # 卡在授权上（on_ask 说要等）
    ERROR = "error"               # 调用不了模型


@dataclass
class Step:
    n: int
    tool: str
    args: dict
    by: By
    ok: bool = True
    #: 给模型看的那段摘要（也是轨迹里给人看的）
    brief: str = ""
    seconds: float = 0.0
    #: 模型在调这个工具前说的话（经常没有，但有时能看出它的意图）
    thought: str = ""
    #: 这一步的备注：重复调用被挡、用户拒了…
    note: str = ""

    def line(self) -> str:
        who = {By.MODEL: "模型自决", By.USER: "你点头", By.AUTO: "授权自决"}[self.by]
        mark = "✓" if self.ok else "✗"
        s = f"  {self.n}. {mark} {self.tool}　（{who}　{self.seconds:.1f}s）"
        if self.note:
            s += f"\n       {self.note}"
        return s


@dataclass
class Trace:
    """一次编排的完整记录 —— **这就是「可回访的判断存档」的骨架**。"""

    goal: str
    model: str
    autonomy: Autonomy
    steps: list[Step] = field(default_factory=list)
    stop: Stop = Stop.DONE
    final: str = ""
    #: 卡住时给用户的一句话（为什么没走完）
    stuck: str = ""

    def render(self) -> str:
        out = [f"目标　{self.goal}",
               f"模型　{self.model}　自主度　{AUTONOMY_LABEL[self.autonomy]}",
               ""]
        if not self.steps:
            out.append("  （一步都没走）")
        for s in self.steps:
            out.append(s.line())
        out.append("")
        out.append(f"结束　{_stop_label(self.stop)}")
        if self.stuck:
            out.append(f"卡在　{self.stuck}")
        if self.final:
            out.append("")
            out.append(self.final)
        return "\n".join(out)

    def save_json(self, path: str) -> None:
        """落盘 —— 与「每次估值留一份可回访的判断存档」同一个用途。"""
        from pathlib import Path
        Path(path).write_text(json.dumps({
            "goal": self.goal, "model": self.model, "autonomy": self.autonomy.value,
            "stop": self.stop.value, "stuck": self.stuck, "final": self.final,
            "steps": [{"n": s.n, "tool": s.tool, "args": s.args, "by": s.by.value,
                       "ok": s.ok, "brief": s.brief, "seconds": s.seconds,
                       "note": s.note} for s in self.steps],
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def _stop_label(s: Stop) -> str:
    return {Stop.DONE: "模型说做完了", Stop.MAX_STEPS: "到步数上限了",
            Stop.REFUSED: "你拒了某一步", Stop.NEEDS_CONSENT: "卡在授权上",
            Stop.ERROR: "调用不了模型"}[s]


SYSTEM = (
    "你是一个投资尽调助手的编排大脑。为了达成用户的目标，"
    "你可以调用下面这些工具，一次一个。\n"
    "规则：\n"
    "1. 一次只调用一个工具。\n"
    "2. **没有明确任务时不要调用任何工具。** 用户只是打招呼、或者话里没有"
    "要处理的材料/数据/请求时，直接回话。为了显得能干而先扫一遍目录，"
    "是明确不要的行为（扫一个几百页的目录要等两分钟）。\n"
    "3. 数据不全时不要硬算 —— 先调 list_gaps 把缺的弄清楚。\n"
    "4. 需要外部访问的操作必须先取得用户授权；缺授权时它会告诉你，"
    "**这时不要重复调同一个工具**，改去问用户或者换个做法。\n"
    "5. 觉得已经够了，就直接用文字给出结论，不要再调工具。\n"
    "6. 已经调过的工具、参数又一样，**不要重复调** —— 结果是一样的。"
)


class LoopError(RuntimeError):
    pass


def _chat(model: str, messages: list[dict], tools: list[dict],
          timeout: int = TIMEOUT) -> dict:
    body = json.dumps({"model": model, "messages": messages, "tools": tools,
                       "stream": False, "think": False,
                       "options": {"temperature": 0}}).encode()
    req = urllib.request.Request(
        OLLAMA_CHAT, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.URLError as exc:
        raise LoopError(f"连不上本机模型服务（{OLLAMA_CHAT}）：{exc}") from exc
    except (json.JSONDecodeError, TimeoutError) as exc:
        raise LoopError(f"模型响应读不了：{type(exc).__name__}: {exc}") from exc


def _calls(resp: dict) -> list[tuple[str, dict]]:
    """把 `tool_calls` 拆成 `[(名字, 参数)]`。**参数解析不了就返回空参数并记下来。**"""
    out = []
    for c in ((resp.get("message") or {}).get("tool_calls") or []):
        fn = c.get("function") or {}
        name = fn.get("name") or ""
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"__坏参数__": args}
        out.append((name, args if isinstance(args, dict) else {}))
    return out


def _key(tool: str, args: dict) -> str:
    try:
        return tool + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return tool + "|" + str(args)


def run(goal: str, *, model: str = DEFAULT_MODEL,
        autonomy: Autonomy = Autonomy.CONFIRM,
        only: list[str] | None = None,
        max_steps: int = 8,
        on_ask: Callable[[str, dict, str], tuple[bool, Any]] | None = None,
        consent: dict[str, Any] | None = None,
        system: str = SYSTEM) -> Trace:
    """跑一遍编排。

    `on_ask(kind, payload, question)` 是**跟用户交互的那一个口子** ——
    循环自己不认识界面，只负责问。返回 `(同意吗, 附带的东西)`：

        kind="confirm"   payload={"tool","args"}      执行前要不要走这一步
        kind="consent"   payload={"hosts","tool"}     要不要授权访问这些主机

    `on_ask=None` 时：`one_click` 一路走；`confirm` 直接停下（没人可问）。
    """
    trace = Trace(goal=goal, model=model, autonomy=autonomy)
    tools = R.schema(only=only)
    if not tools:
        trace.stop = Stop.ERROR
        trace.stuck = "一个工具都没有注册"
        return trace

    messages: list[dict] = [{"role": "system", "content": system},
                            {"role": "user", "content": goal}]
    #: 已经调过的（工具+参数）→ 第几步。用来挡重复调用。
    seen: dict[str, int] = {}
    consent = dict(consent or {})

    for n in range(1, max_steps + 1):
        try:
            resp = _chat(model, messages, tools)
        except LoopError as exc:
            trace.stop = Stop.ERROR
            trace.stuck = str(exc)
            return trace

        calls = _calls(resp)
        text = ((resp.get("message") or {}).get("content") or "").strip()

        if not calls:
            # 模型不调工具了 —— 认为它做完了
            trace.stop = Stop.DONE
            trace.final = text
            return trace

        # 一次只走一个（规则这么写的，模型多给了就只取第一个并记下来）
        name, args = calls[0]
        step = Step(n=n, tool=name, args=args, by=By.MODEL)
        if len(calls) > 1:
            step.note = f"模型一次给了 {len(calls)} 个调用，只走第一个：{name}"

        # ① 重复调用挡掉 —— **不真的再跑一遍**（扫描很贵）
        k = _key(name, args)
        if k in seen:
            step.ok = False
            step.note = (f"重复调用（第 {seen[k]} 步已经调过同样的参数）"
                         f"—— 没重新执行，直接把上次的结果还给它")
            step.seconds = 0.0
            trace.steps.append(step)
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": name, "arguments": args}}]})
            messages.append({"role": "tool", "tool_name": name,
                             "content": "这个调用刚才已经做过，结果一样，"
                                        "不要重复调。换个做法，或者直接给结论。"})
            continue

        # ② 走每一步之前问用户（confirm 模式）
        if autonomy is Autonomy.CONFIRM and on_ask is not None:
            t = R.get(name)
            what = t.summary if t else "**未注册的工具**"
            allow, _ = on_ask("confirm", {"tool": name, "args": args},
                              f"下一步：{name} —— {what}")
            step.by = By.USER
            if not allow:
                step.ok = False
                step.note = "你拒了这一步"
                trace.steps.append(step)
                trace.stop = Stop.REFUSED
                trace.stuck = f"你在第 {n} 步拒了 {name}"
                return trace
        elif autonomy is Autonomy.ONE_CLICK:
            step.by = By.AUTO

        # ③ 执行（出网缺授权时返回 needs_consent，交给 on_ask）
        t0 = time.time()
        result = R.call(name, args, consent=consent)
        if result.needs_consent and on_ask is not None:
            allow, extra = on_ask(
                "consent", {"hosts": result.hosts, "tool": name},
                f"{name} 要访问：{'、'.join(result.hosts)}。同意这一次去取吗？")
            if allow:
                if isinstance(extra, dict):
                    consent.update(extra)
                result = R.call(name, args, consent=consent)
            else:
                step.ok = False
                step.note = f"你拒了授权（{'、'.join(result.hosts)}）"
                trace.steps.append(step)
                trace.stop = Stop.NEEDS_CONSENT
                trace.stuck = f"{name} 需要授权访问 {'、'.join(result.hosts)}"
                return trace
        step.seconds = time.time() - t0
        step.ok = result.ok
        step.brief = result.brief()
        trace.steps.append(step)
        seen[k] = n

        # ④ 结果喂回去
        messages.append({"role": "assistant", "content": text, "tool_calls": [
            {"function": {"name": name, "arguments": args}}]})
        messages.append({"role": "tool", "tool_name": name,
                         "content": step.brief})

    trace.stop = Stop.MAX_STEPS
    trace.stuck = (f"走到 {max_steps} 步上限还没给结论 —— "
                   f"可能是卡在某个反复失败的调用上，也可能是目标太大")
    return trace
