"""假设参谋的 CLI —— 接进 `value.py` 的报告流。

配置写在估值配置里，跑完估值后自动追加一节：

```json
"advisor": {
  "peer_sets": {
    "汽车零部件（美股）": {
      "tickers": ["LEA", "MGA", "BWA", "DAN"],
      "metric": "revenue_cagr",
      "years": 3,
      "as_of": "2025-12-31"
    }
  },
  "review": [
    {"name": "2027 年收入增长", "value": 0.146,
     "source": "管理层规划 p.12", "confidence": "低",
     "peer_set": "汽车零部件（美股）"}
  ],
  "terminal_growth": {
    "g": {"value": 0.025, "source": "管理层"},
    "long_term_nominal_gdp": {"value": 0.045, "source": "社科院长期展望"},
    "long_term_inflation": {"value": 0.02, "source": "央行目标"}
  }
}
```

**`peer_set` 是可选的** —— 不给就只输出证据清单，不输出分位数。
那不是缺陷：拿不到同行分布时，**不凑一个参照系**才是对的。
"""

from __future__ import annotations

from typing import Any

from datasources import sec_edgar as se

from . import advisor as ad
from .core import Assumption, Confidence

CONF_MAP = {"高": Confidence.HIGH, "中": Confidence.MEDIUM,
            "低": Confidence.LOW, "缺失": Confidence.MISSING}


class AdvisorConfigError(ValueError):
    """配置写错了。**指出位置，不猜用户想写什么。**"""


def _assumption(spec: Any, name: str, unit: str = "") -> Assumption:
    if spec is None:
        return Assumption(name, None, unit, "未提供", Confidence.MISSING)
    if isinstance(spec, dict):
        if "value" not in spec:
            raise AdvisorConfigError(f"{name}：写成了对象但没有 value 字段 → {spec}")
        return Assumption(
            name=name,
            value=spec["value"],
            unit=spec.get("unit", unit),
            source=spec.get("source", "未注明"),
            confidence=CONF_MAP.get(spec.get("confidence", "中"), Confidence.MEDIUM),
        )
    return Assumption(name, float(spec), unit, "未注明（裸数字）", Confidence.LOW)


def _build_peer_sets(cfg: dict) -> dict[str, ad.PeerStat]:
    out: dict[str, ad.PeerStat] = {}
    for label, spec in (cfg.get("peer_sets") or {}).items():
        tickers = spec.get("tickers")
        if not tickers:
            raise AdvisorConfigError(
                f"peer_sets.{label}：要给 tickers 列表。"
                f"（**目前只支持美股** —— A 股/港股还没有免接口的数据源）"
            )
        pairs: list[tuple[str, str]] = []
        missing: list[str] = []
        for t in tickers:
            cik = se.ticker_to_cik(t)
            if cik is None:
                missing.append(t)
            else:
                pairs.append((cik, t))
        if missing:
            raise AdvisorConfigError(
                f"peer_sets.{label}：这些代码在 SEC 查不到 → {missing}。\n"
                f"    **目前只支持美股** —— A 股/港股还没有免接口的数据源，"
                f"所以取不到。请换成美股代码，或把这一项留空。\n"
                f"   （留空不是缺陷：拿不到同行分布时，不凑一个参照系才是对的。）"
            )
        out[label] = ad.build_peer_stat(
            pairs,
            metric=spec.get("metric", "revenue_cagr"),
            years=int(spec.get("years", 3)),
            as_of=spec.get("as_of"),
        )
    return out


def run_advisor(cfg: dict, out: list[str], title: str = "假设参谋") -> None:
    """把参谋结果追加到报告里。就地修改 out。"""
    if "advisor" not in cfg:
        return
    acfg = cfg["advisor"] or {}

    out.append("")
    out.append("═" * 78)
    out.append(f"{title} —— 每个关键假设配一个对照")
    out.append("═" * 78)

    try:
        peer_sets = _build_peer_sets(acfg)
    except (AdvisorConfigError, ValueError) as e:
        out.append(f"  同行分布取数失败：{e}")
        out.append("  （证据清单和永续增长检查仍然会给出）")
        peer_sets = {}

    review = acfg.get("review") or []
    if not review:
        out.append("")
        out.append("  没有配置要复核的假设（advisor.review 为空）")
    for item in review:
        if "name" not in item:
            raise AdvisorConfigError(f"advisor.review 的每一项都要有 name → {item}")
        a = _assumption(
            {k: v for k, v in item.items() if k != "peer_set"},
            item["name"],
            item.get("unit", ""),
        )
        stat = peer_sets.get(item.get("peer_set")) if item.get("peer_set") else None
        if item.get("peer_set") and stat is None:
            out.append("")
            out.append(f"⚠ 找不到 peer_set「{item['peer_set']}」，"
                       f"这一项只给证据清单，不给分位数")
        out.append("")
        out.append("─" * 78)
        out.append(ad.advise(a, peer_stat=stat).render())

    tg = acfg.get("terminal_growth")
    if tg:
        g = _assumption(tg.get("g"), "永续增长率")
        gdp = _assumption(tg.get("long_term_nominal_gdp"), "长期名义GDP增速")
        infl = _assumption(tg.get("long_term_inflation"), "长期通胀预期") \
            if tg.get("long_term_inflation") else None
        out.append("")
        out.append("─" * 78)
        out.append("永续增长的硬边界检查")
        out.append(f"  假设：{g.value:.2%}" if g.value is not None else "  假设：未提供")
        out.append(f"  上界：长期名义 GDP 增速 {gdp.value:.2%}"
                   if gdp.value is not None
                   else "  上界：长期名义 GDP 增速未提供")
        problems = ad.terminal_growth_check(g, gdp, infl)
        if problems:
            for p in problems:
                out.append(f"  ⚠ {p}")
        else:
            out.append("  ✓ 通过（未超长期名义 GDP 增速，且不低于长期通胀）")
