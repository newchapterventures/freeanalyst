"""早期项目估值的 CLI。

`value.py` 在配置里看到 `early_stage` 段时会把活交给这里。
输入形态和财务报表那套完全不同 —— 这边是评分和情景，不是三张表。

## 输入 JSON 的样子

```json
{
  "target": "某 AI 医疗影像公司",
  "scenario": {"stage": "早期项目", "purpose": "融资定价", ...},
  "early_stage": {
    "unit": "万元",
    "berkus":    { "per_factor_cap": {...}, "factors": {...} },
    "scorecard": { "base": {...}, "factors": {...} },
    "vc_method": { "exit_value": {...}, ..., "reverse": true },
    "first_chicago": { "outcomes": [...] }
  }
}
```

**每个数字都可以带来源和置信度**，写法：

    "exit_value": {"value": 120000, "source": "行业可比交易", "confidence": "中"}
    "exit_value": 120000        ← 裸数字会被标为低置信度，因为没有来源

区间写法（low/mid/high）：

    "管理团队实力": {"low": 1.1, "mid": 1.2, "high": 1.3, "source": "..."}
"""

from __future__ import annotations

from typing import Any

from valuation import Assumption, Confidence
from valuation import early_stage as es
from valuation.core import ValuationResult

UNIT = "万元"

CONF_MAP = {"高": Confidence.HIGH, "中": Confidence.MEDIUM,
            "低": Confidence.LOW, "缺失": Confidence.MISSING}


class ConfigError(ValueError):
    """配置文件写错了。**直接报错并指出位置，不要猜用户想写什么。**"""


def _assumption(spec: Any, name: str, unit: str = UNIT) -> Assumption:
    """JSON 里的一个数 → Assumption。裸数字标为低置信度。"""
    if spec is None:
        return Assumption(name, None, unit, "未提供", Confidence.MISSING)
    if isinstance(spec, dict):
        if "value" not in spec:
            raise ConfigError(f"{name}：写成了对象但没有 value 字段 → {spec}")
        return Assumption(
            name=name,
            value=spec["value"],
            unit=spec.get("unit", unit),
            source=spec.get("source", "未注明"),
            confidence=CONF_MAP.get(spec.get("confidence", "中"), Confidence.MEDIUM),
            note=spec.get("note", ""),
        )
    return Assumption(name, float(spec), unit, "未注明（裸数字）", Confidence.LOW)


def _range(spec: Any, name: str) -> es.Range:
    """JSON 里的一个数或区间 → Range。"""
    if spec is None:
        raise ConfigError(f"{name}：缺了")
    if isinstance(spec, (int, float)):
        return es.Range.point(float(spec))
    if isinstance(spec, dict):
        if "mid" in spec:
            mid = float(spec["mid"])
            return es.Range(float(spec.get("low", mid)), mid,
                            float(spec.get("high", mid)))
        if "value" in spec:
            return es.Range.point(float(spec["value"]))
    if isinstance(spec, list):
        if len(spec) != 3:
            raise ConfigError(f"{name}：区间要写成 [低, 中, 高] 三个数，收到 {spec}")
        return es.Range(float(spec[0]), float(spec[1]), float(spec[2]))
    raise ConfigError(f"{name}：不认识的写法 {spec!r}")


def _factor(name: str, spec: Any, unit: str = "") -> es.Factor:
    if isinstance(spec, dict) and ("mid" in spec or "low" in spec
                                   or "high" in spec or "value" in spec):
        rng = _range(spec, name)
        src = spec.get("source", "未注明")
        conf = CONF_MAP.get(spec.get("confidence", "低"), Confidence.LOW)
        note = spec.get("note", "")
    else:
        rng = _range(spec, name)
        src, conf, note = "未注明（裸数字）", Confidence.LOW, ""
    return es.Factor(name, rng, source=src, confidence=conf, note=note)


# ---------------------------------------------------------------------------

def _build_berkus(cfg: dict, unit: str) -> ValuationResult | None:
    if "berkus" not in cfg:
        return None
    b = cfg["berkus"]
    cap = b.get("per_factor_cap")
    if cap is None:
        raise ConfigError(
            "berkus.per_factor_cap 必须显式给 —— 它是整个 Berkus 法的锚点，"
            "没有默认值。写清楚这个数从哪来的（年份、地区、项目阶段）。"
        )
    cap_rng = _range(
        cap.get("range") if isinstance(cap, dict) and "range" in cap else cap,
        "每因素封顶值",
    )
    cap_src = cap.get("source", "未注明") if isinstance(cap, dict) else "未注明（裸数字）"
    cap_conf = (CONF_MAP.get(cap.get("confidence", "低"), Confidence.LOW)
                if isinstance(cap, dict) else Confidence.LOW)

    raw_factors = b.get("factors")
    if not raw_factors:
        raise ConfigError("berkus.factors 不能为空")
    factors = [_factor(n, s) for n, s in raw_factors.items()]

    rev = None
    if "revenue_component" in b:
        rev = _assumption(b["revenue_component"], "收入加项", unit)

    return es.berkus(factors, cap_rng, cap_source=cap_src,
                     cap_confidence=cap_conf, revenue_component=rev, unit=unit)


def _build_scorecard(cfg: dict, unit: str) -> ValuationResult | None:
    if "scorecard" not in cfg:
        return None
    s = cfg["scorecard"]
    if "base" not in s:
        raise ConfigError(
            "scorecard.base 必须显式给 —— 「本地区本阶段平均 pre-money」"
            "是外部事实，不是能从标的数据算出来的。而且时效性极强，"
            "务必注明是哪一年的数据。"
        )
    base_spec = s["base"]
    base_rng = _range(base_spec, "区域基准 pre-money")
    base_src = base_spec.get("source", "未注明") if isinstance(base_spec, dict) else "未注明（裸数字）"
    base_conf = (CONF_MAP.get(base_spec.get("confidence", "低"), Confidence.LOW)
                 if isinstance(base_spec, dict) else Confidence.LOW)

    raw = s.get("factors")
    if not raw:
        raise ConfigError("scorecard.factors 不能为空")
    # 传 Factor 对象而不是裸 Range —— 否则因素自己的来源和置信度会丢
    factors = {n: _factor(n, v) for n, v in raw.items()}
    return es.scorecard(base_rng, base_src, factors,
                        base_confidence=base_conf, unit=unit)


def _build_vc(cfg: dict, unit: str) -> list[ValuationResult]:
    if "vc_method" not in cfg:
        return []
    v = cfg["vc_method"]

    kw = {}
    for key in ("exit_value", "investment", "years_to_exit",
                "target_multiple", "target_irr", "future_dilution"):
        if key in v:
            kw[key] = _assumption(v[key], key, "倍率" if "multiple" in key else
                                  "年" if "year" in key else unit)
    if "target_multiple" not in kw and "target_irr" not in kw:
        raise ConfigError("vc_method 里 target_multiple 和 target_irr 至少要有一个")

    out = [es.vc_method(unit=unit, **kw)]

    # 有报价就必须跑反向（§6.6）
    if v.get("reverse"):
        ask = v.get("ask_price")
        if ask is None:
            raise ConfigError(
                "vc_method.reverse 开了，但没给 ask_price（对方报价的投前估值）"
            )
        ask_a = _assumption(ask, "对方报价 pre-money", unit)
        d = kw.get("future_dilution")
        out.append(es.vc_required_exit(
            pre_money=ask_a,
            investment=kw["investment"],
            years_to_exit=kw["years_to_exit"],
            target_multiple=kw["target_multiple"],
            future_dilution=d,
            unit=unit,
        ))
    return out


def _build_first_chicago(cfg: dict, unit: str) -> ValuationResult | None:
    if "first_chicago" not in cfg:
        return None
    fc = cfg["first_chicago"]
    outs = []
    for o in fc.get("outcomes", []):
        if "label" not in o or "probability" not in o or "value" not in o:
            raise ConfigError(f"情景要写全 label / probability / value，收到 {o}")
        outs.append(es.Outcome(o["label"], float(o["probability"]),
                               float(o["value"]), o.get("basis", "")))
    return es.first_chicago(outs, unit=unit, note=fc.get("note", ""))


# ---------------------------------------------------------------------------

def run_early_stage(cfg: dict, show_trace: bool = True) -> int:
    """跑完早期项目的整条流程，打印报告。"""
    es_cfg = cfg["early_stage"] or {}
    unit = es_cfg.get("unit", UNIT)

    out: list[str] = []
    out.append("=" * 78)
    out.append(f"早期项目估值 —— {cfg.get('target', '未命名标的')}")
    out.append("=" * 78)
    if cfg.get("note"):
        out.append(f"说明：{cfg['note']}")
    out.append(
        "方法：Berkus（去掉了多少风险）· Scorecard（比市场同类好还是差）"
        "· VC 法（按目标回报今天最多付多少）"
    )

    results: list[ValuationResult] = []
    try:
        for builder in (_build_berkus, _build_scorecard):
            r = builder(es_cfg, unit)
            if r:
                results.append(r)
        results.extend(_build_vc(es_cfg, unit))
        fc = _build_first_chicago(es_cfg, unit)
        if fc:
            results.append(fc)
    except (es.EarlyStageError, ConfigError) as e:
        print(f"\n输入有问题，算不了：\n  {e}", flush=True)
        return 1

    if not results:
        print("\nearly_stage 里什么都没配（berkus / scorecard / vc_method / "
              "first_chicago 至少要有一个）", flush=True)
        return 1

    for r in results:
        out.append("")
        out.append("─" * 78)
        out.append(f"{r.method}")
        out.append("─" * 78)
        out.append(f"  结果：{r.range_text}")
        if show_trace:
            out.append("")
            out.append("  计算追溯：")
            out.append(r.trace.render())
        if r.notes:
            out.append("")
            for n in r.notes:
                out.append(f"  · {n}")

    # 多方法交叉 —— 差额必须报出来
    if len(results) > 1:
        out.append("")
        out.append("═" * 78)
        out.append("多方法对照")
        out.append("═" * 78)
        out.append(es.compare(results, unit))

    # 缺口与低置信度（强制输出）
    all_a: list[Assumption] = []
    for r in results:
        all_a.extend(r.assumptions)

    out.append("")
    out.append("═" * 78)
    out.append("数据缺口（必须补）")
    out.append("═" * 78)
    gaps = [a for a in all_a if a.is_missing]
    if gaps:
        for name in dict.fromkeys(a.name for a in gaps):
            out.append(f"  ✗ {name}")
    else:
        out.append("  无")

    out.append("")
    out.append("═" * 78)
    out.append("低置信度假设（结果的软肋）")
    out.append("═" * 78)
    low = [a for a in all_a if a.confidence is Confidence.LOW]
    if low:
        seen: set[str] = set()
        for a in low:
            if a.name in seen:
                continue
            seen.add(a.name)
            out.append(f"  ! {a.name} = {a.value}  ← 来源：{a.source}")
    else:
        out.append("  无")

    out.append("")
    print("\n".join(out), flush=True)
    return 0
