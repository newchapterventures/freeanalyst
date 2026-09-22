"""模型质量门槛 —— **装完先测，不合格就明说不合格**。

## 用户的原话（2026-09-17）

> 鉴于开源大模型参数少，**我们要保证开源大模型达到我们想要的结果**。

## 这条门槛在解决什么

「能跑」和「能用」是两件事。小模型能让流程跑通，但会犯三类**致命**错误：

  1. **主体搞混** —— 把实控人的个人回购义务说成公司的财务压力
  2. **跨文档冲突检测不出来** —— CIM 说条款已定，访谈纪要里对方其实有保留
  3. **编造内容** —— 材料里没有的文件，它替你把结论写出来

这三类在尽调里会**直接导致错误结论**。所以门槛不是「越强越好」，
是**这三类错一个都不能犯**。

判分逻辑在 `bench/model_quality.py`（按节解析 + 来源覆盖 + 语义模式）。
这里只做**调度和裁决** —— 不重复实现判据，
免得两边判据不一致，出现「bench 说够格、门槛说不够格」。

## 一条自检规矩

换判据之前先拿**已知不合格的模型**跑一遍 ——
**如果它全过，说明评测失效了**，不是模型变强了。
这条规矩在 `bench/model_quality.py` 里已经写了，这里调用时不覆盖它。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench" / "model_quality.py"

#: 门槛：**全过才算合格。** 不是「及格线」，是「这三类错一个都不能犯」。
PASS_ALL = True


@dataclass
class GateResult:
    model: str
    passed: bool
    #: 例如 "5/5"
    score: str = ""
    #: 没过的话卡在哪一条
    failed: list[str] = field(default_factory=list)
    #: 评测报告落在哪
    report: str = ""
    #: 跑不了的原因（模型没装、超时…）
    error: str = ""

    def line(self) -> str:
        if self.error:
            return f"{self.model}　✗ 测不了：{self.error}"
        if self.passed:
            return f"{self.model}　★ 够格进生产（{self.score}）"
        return f"{self.model}　✗ 未达门槛（{self.score}）"


def detail_lines(r: GateResult) -> list[str]:
    """没过的话，把**卡在哪几条、为什么**摆出来。

    只说「未达门槛」没用 —— 用户要知道是「主体搞混」还是「编造内容」，
    这两类的严重程度和处理方式不一样。
    """
    if not r.failed:
        return []
    return ["      卡在："] + [f"        ✗ {f}" for f in r.failed]


def run_gate(model: str, *, timeout: int = 1800,
             report_dir: Path | None = None) -> GateResult:
    """跑一遍质量评测，给出裁决。

    **不自己判分** —— 判分逻辑只有一份，在 `bench/model_quality.py`。
    这里只是把它的结论搬过来，并加上「没过就说清卡在哪」。
    """
    if not BENCH.exists():
        return GateResult(model=model, passed=False,
                          error=f"找不到评测脚本 {BENCH}")

    report_dir = report_dir or (ROOT / "out")
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / f"gate-{_safe(model)}.json"

    try:
        proc = subprocess.run(
            [sys.executable, str(BENCH), "--models", model, "--json", str(report)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return GateResult(model=model, passed=False,
                          error=f"评测超过 {timeout} 秒还没跑完")
    except OSError as exc:                                    # noqa: BLE001
        return GateResult(model=model, passed=False, error=str(exc))

    out = (proc.stdout or "") + (proc.stderr or "")
    return _verdict(model, out, report)


def _verdict(model: str, out: str, report: Path) -> GateResult:
    """从评测结果里读结论。**优先读 JSON，读不到才回落到抓 stdout。**

    评测自己会写一份结构化报告（`bench/model_quality.py --json`），
    里面每条用例的 `passed` 和 `reasons` 都是现成的 ——
    抓 stdout 是在猜它的打印格式，改个措辞就失效。
    """
    from_json = _from_json(model, report)
    if from_json is not None:
        return from_json

    # 回落：门槛脚本自己的措辞（bench/model_quality.py）
    if "★ 够格进生产" in out:
        return GateResult(model=model, passed=True,
                          score=_score(out), report=str(report))
    if "未达门槛" in out:
        return GateResult(model=model, passed=False,
                          score=_score(out), failed=_failed(out),
                          report=str(report))
    tail = out.strip().splitlines()[-3:] if out.strip() else ["（没有任何输出）"]
    return GateResult(model=model, passed=False,
                      error="评测没给出结论： " + " / ".join(tail))


def _from_json(model: str, report: Path) -> GateResult | None:
    """读评测写出来的结构化报告。读不到/格式不对就返回 None（交给回落）。"""
    if not report.exists():
        return None
    try:
        raw = json.loads(report.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    # 报告按模型名分组；取不到的用第一个（单模型跑的时候就是这个）
    d = raw.get(model) or next(iter(raw.values()), None)
    if not isinstance(d, dict) or "passed" not in d:
        return None

    got, total = d.get("passed", 0), d.get("total", 0)
    failed = []
    for r in d.get("results") or []:
        if r.get("passed"):
            continue
        why = "；".join(r.get("reasons") or []) or "未说明原因"
        failed.append(f"{r.get('name', r.get('id', '?'))} —— {why}")
    return GateResult(model=model, passed=(total > 0 and got == total),
                      score=f"{got}/{total}", failed=failed[:6],
                      report=str(report))


def _score(out: str) -> str:
    m = re.search(r"(\d+)\s*/\s*(\d+)", out)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def _failed(out: str) -> list[str]:
    """找出卡在哪几条。读不到就不列 —— 宁可只说「未达门槛」。"""
    names = []
    for line in out.splitlines():
        # 形如 "  ✗ 主体识别：..."
        if "✗" in line or "未通过" in line:
            txt = line.strip().lstrip("✗ ").strip()
            if txt:
                names.append(txt[:60])
    return names[:5]


def _safe(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", model)


def report_lines(results: list[GateResult]) -> list[str]:
    """给用户看的裁决书。"""
    out = []
    ok = [r for r in results if r.passed]
    bad = [r for r in results if not r.passed]
    for r in results:
        out.append("  " + r.line())
        out.extend(detail_lines(r))
    out.append("")
    if ok:
        out.append(f"  **可用：{', '.join(r.model for r in ok)}**")
    else:
        out.append("  **没有一个模型达到门槛。**")
        out.append("    这时候正确的做法不是「先凑合用」—— 尽调里这三类错误")
        out.append("    会直接导致错误结论。要么换个更大的模型，")
        out.append("    要么只用不需要模型的功能（提取和估值是纯代码算的）。")
    for r in bad:
        if r.report:
            out.append(f"    {r.model} 的评测明细：{r.report}")
    return out
