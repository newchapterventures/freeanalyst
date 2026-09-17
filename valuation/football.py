"""Football Field —— 把各估值方法的区间汇总到一张图上。

## 它是什么

一级市场/投行做估值结论时最常用的一张图：每个方法画成一条**水平区间条**，
全部叠在同一条「每股价值」坐标轴上，再画一条竖线标出基准价（当前股价 /
要约价 / 上一轮融资价）。

                          每股价值
    DCF            ────██████████──────
    可比公司       ─────────█████████──────
    可比交易       ────████████████────────
    净资产         ──█████─────────────────
                           ┃
                        基准价

它解决的问题是**「不同方法给出不同的数，到底信哪个」** —— 不选一个，
而是把区间并排摆出来，看**共识区间**在哪。

## 设计纪律（都是前面定下来的）

**一、区间取敏感性网格的极值，不取点值。**
点值会给人一种「算得准」的错觉。`run_dcf(range_grid=...)` 已经这么做。

**二、跨度超过中值 50% 要报警。**
一个宽到没用的区间等于没算。

**三、每条区间必须带来源和置信度。**
Football Field 上少一条来源，这张图就退化成了「我觉得值这么多」。

**四、`is_valuation=False` 的方法不进图。**
反向 VC 法算的是「这个价需要什么条件才成立」，不是估值。
放进 Football Field 会误导。

**五、缺数据不许默认值填充。**
算不出区间的，**明写算不出**，不画一条假条。

## 和数据/渲染解耦

`FootballField` 是纯数据，渲染成 HTML 还是 Excel 都能读它。
用户原话「生成之后可手动编辑的最好」—— 那多半是 Excel，
但初级形态先出 HTML。**换渲染器不该动计算。**
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path

#: 跨度超过中值这个比例就报警
SPAN_ALERT = 0.50


@dataclass
class Band:
    """Football Field 上的一条区间。"""

    method: str
    low: float | None = None
    mid: float | None = None
    high: float | None = None
    #: 怎么算出来的（一句话）
    basis: str = ""
    #: 用了哪些数据，各自哪来的
    sources: list[str] = field(default_factory=list)
    #: 高 / 中 / 低
    confidence: str = "中"
    #: 这一条有什么要提醒的
    note: str = ""

    @property
    def usable(self) -> bool:
        return None not in (self.low, self.mid, self.high)

    @property
    def span(self) -> float | None:
        if not self.usable or not self.mid:
            return None
        return (self.high - self.low) / abs(self.mid)

    def render_line(self) -> str:
        if not self.usable:
            return f"  {self.method:12} —（{self.note or '算不出'}）"
        warn = ""
        if self.span is not None and self.span > SPAN_ALERT:
            warn = f"  ⚠ 跨度 {self.span:.0%}"
        return (f"  {self.method:12} {self.low:>12,.2f} – {self.high:>12,.2f}"
                f"   中枢 {self.mid:>12,.2f}{warn}")


@dataclass
class FootballField:
    """一张 Football Field 的全部数据。**纯数据，不含渲染。**"""

    entity: str = ""
    currency: str = "人民币"
    unit: str = "元/股"
    #: 总股本（用来把股权价值折成每股）
    shares: float | None = None
    bands: list[Band] = field(default_factory=list)
    #: 基准价：当前股价 / 要约价 / 上一轮融资价
    reference: float | None = None
    reference_label: str = ""
    as_of: str = ""
    #: 口径、来源等在图上要说清的东西
    notes: list[str] = field(default_factory=list)

    def usable_bands(self) -> list[Band]:
        return [b for b in self.bands if b.usable]

    def consensus(self) -> tuple[float, float] | None:
        """所有可用区间的**交集**。

        没有交集时返回 None —— 那本身是个重要结论（各方法分歧太大），
        **不要强行取平均糊过去**。
        """
        b = self.usable_bands()
        if not b:
            return None
        lo, hi = max(x.low for x in b), min(x.high for x in b)
        return (lo, hi) if lo <= hi else None

    def render_text(self) -> str:
        out = [f"  {self.entity}   Football Field   单位：{self.unit}"]
        if self.as_of:
            out[0] += f"   基准日 {self.as_of}"
        out.append("")
        for b in self.bands:
            out.append(b.render_line())
        out.append("")
        if self.reference is not None:
            out.append(f"  {self.reference_label or '基准价':12} {self.reference:>12,.2f}")
        else:
            out.append(f"  ⚠ 没有基准价 —— Football Field 的竖线画不出来")
        c = self.consensus()
        out.append("")
        if c:
            out.append(f"  共识区间     {c[0]:>12,.2f} – {c[1]:>12,.2f}")
        else:
            out.append("  ⚠ **各方法没有交集** —— 分歧过大，本身是个结论，"
                       "不要取平均糊过去")
        for n in self.notes:
            out.append(f"  · {n}")
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {
            "entity": self.entity, "currency": self.currency, "unit": self.unit,
            "shares": self.shares, "reference": self.reference,
            "reference_label": self.reference_label, "as_of": self.as_of,
            "notes": self.notes,
            "bands": [
                {"method": b.method, "low": b.low, "mid": b.mid, "high": b.high,
                 "basis": b.basis, "sources": b.sources,
                 "confidence": b.confidence, "note": b.note,
                 "span": b.span}
                for b in self.bands
            ],
        }


def from_result(method: str, result, shares: float | None = None,
                basis: str = "", sources: list[str] | None = None,
                confidence: str = "中") -> Band:
    """把估值引擎的 `ValuationResult` 折成一条区间。

    ## 两个必须拦下的情况

    **`is_valuation=False`（反向 VC 法）不进图。** 它算的是「这个价需要什么
    条件才成立」，不是估值 —— 放进 Football Field 会被当成一个估值意见。

    **没有股数就算不出每股。** 返回一条 `usable=False` 的区间并说明原因，
    **不画一条假的**。
    """
    if not getattr(result, "is_valuation", True):
        return Band(method=method,
                    note="不是估值（反向法算的是「什么条件成立」，不是值多少），"
                         "不进 Football Field")

    if shares is None or shares <= 0:
        return Band(method=method, basis=basis,
                    note="**没有总股本** —— 算不出每股价值；"
                         "股权价值给到了但没有股数，不能拿总金额当每股")

    lo = getattr(result, "low", None)
    mid = getattr(result, "mid", None)
    hi = getattr(result, "high", None)
    if None in (lo, mid, hi):
        return Band(method=method, basis=basis, note="估值引擎没给出完整区间")

    return Band(method=method, low=lo / shares, mid=mid / shares, high=hi / shares,
                basis=basis or getattr(result, "method", ""),
                sources=sources or [], confidence=confidence)


def build(entity: str, bands: list[Band], shares: float | None = None,
          reference: float | None = None, reference_label: str = "",
          currency: str = "人民币", unit: str = "元/股", as_of: str = "",
          notes: list[str] | None = None) -> FootballField:
    ff = FootballField(entity=entity, currency=currency, unit=unit, shares=shares,
                       bands=list(bands), reference=reference,
                       reference_label=reference_label, as_of=as_of,
                       notes=list(notes or []))

    usable = ff.usable_bands()
    if shares is None and usable:
        ff.notes.append("⚠ 没有总股本 —— 下列区间是按**股权价值**给的，"
                        "不是每股，和基准价不可比")
    for b in ff.bands:
        if b.span is not None and b.span > SPAN_ALERT:
            ff.notes.append(
                f"⚠ {b.method} 的区间跨度 {b.span:.0%}（超过 {SPAN_ALERT:.0%}）——"
                f"这个模型对该方法的输入极为敏感，区间宽到不好用")
    if reference is not None and usable:
        below = [b.method for b in usable if b.high < reference]
        above = [b.method for b in usable if b.low > reference]
        if below:
            ff.notes.append(f"全部低于基准价：{'、'.join(below)}")
        if above:
            ff.notes.append(f"全部高于基准价：{'、'.join(above)}")
    return ff


# ---------------------------------------------------------------------------
# 渲染：HTML（初级形态）
# ---------------------------------------------------------------------------

_CSS = """
.ff { font-family: inherit; color: var(--foreground); }
.ff h3 { margin: 0 0 .2em; font-size: 1.05em; font-weight: 600; }
.ff .sub { color: var(--muted-foreground); font-size: .82em; margin-bottom: 1em; }
.ff table { border-collapse: collapse; width: 100%; font-size: .85em; }
.ff td { padding: .28em .5em; vertical-align: middle; }
.ff td.name { white-space: nowrap; color: var(--foreground); }
.ff td.num { white-space: nowrap; font-variant-numeric: tabular-nums;
             color: var(--muted-foreground); text-align: right; }
.ff td.track { width: 100%; min-width: 220px; position: relative; height: 1.5em; }
.ff .bar { position: absolute; height: .85em; top: .33em; border-radius: 3px;
           background: var(--accent); opacity: .55; }
.ff .mid { position: absolute; width: 2px; height: 1.1em; top: .2em;
           background: var(--foreground); }
.ff .ref { position: absolute; width: 2px; height: 100%; top: 0;
           background: #d9534f; z-index: 2; }
.ff .axis { position: relative; height: 1.3em; margin-top: .35em;
            color: var(--muted-foreground); font-size: .78em; }
.ff .tick { position: absolute; transform: translateX(-50%); }
.ff .dead { color: var(--muted-foreground); font-style: italic; }
.ff .notes { margin-top: 1em; color: var(--muted-foreground); font-size: .82em;
             line-height: 1.6; }
.ff .cons { margin-top: .6em; font-size: .88em; }
"""


def render_html(ff: FootballField, path: str | Path | None = None) -> str:
    """渲染成 HTML。

    ## 为什么先出 HTML

    用户说「最终要能手动编辑，初级形态哪个快用哪个」。HTML 最快，
    而且能直接在桌面端看。**但渲染器和数据是分开的** ——
    以后要换 Excel，`FootballField` 不用动。
    """
    usable = ff.usable_bands()
    if usable:
        lo = min(b.low for b in usable)
        hi = max(b.high for b in usable)
    else:
        lo, hi = 0.0, 1.0
    if ff.reference is not None:
        lo, hi = min(lo, ff.reference), max(hi, ff.reference)
    span = (hi - lo) or 1.0
    pad = span * 0.04
    lo, hi, span = lo - pad, hi + pad, span + 2 * pad

    def pct(v: float) -> float:
        return (v - lo) / span * 100

    rows = []
    for b in ff.bands:
        if b.usable:
            rows.append(
                f'<tr><td class="name">{html.escape(b.method)}</td>'
                f'<td class="track">'
                f'<div class="bar" style="left:{pct(b.low):.2f}%;'
                f'width:{max(pct(b.high) - pct(b.low), 0.4):.2f}%"></div>'
                f'<div class="mid" style="left:{pct(b.mid):.2f}%"></div>'
                + (f'<div class="ref" style="left:{pct(ff.reference):.2f}%"></div>'
                   if ff.reference is not None else "")
                + f'</td>'
                f'<td class="num">{b.low:,.2f} – {b.high:,.2f}</td></tr>')
        else:
            rows.append(
                f'<tr><td class="name">{html.escape(b.method)}</td>'
                f'<td class="track"></td>'
                f'<td class="num dead">{html.escape(b.note or "算不出")}</td></tr>')

    # 坐标轴：5 个刻度
    ticks = "".join(
        f'<span class="tick" style="left:{i * 25:.1f}%">'
        f'{lo + span * i / 4:,.2f}</span>' for i in range(5))

    ref = ""
    if ff.reference is not None:
        ref = (f'<div class="cons">基准：{html.escape(ff.reference_label or "基准价")} '
               f'<b>{ff.reference:,.2f}</b> {html.escape(ff.unit)}</div>')

    c = ff.consensus()
    cons = ""
    if c:
        cons = (f'<div class="cons">共识区间 <b>{c[0]:,.2f} – {c[1]:,.2f}</b>'
                f' {html.escape(ff.unit)}</div>')
    elif usable:
        cons = ('<div class="cons">⚠ <b>各方法没有交集</b> —— 分歧过大，'
                '本身是个结论，不要取平均糊过去</div>')

    notes = ""
    if ff.notes:
        notes = '<div class="notes">' + "<br>".join(
            html.escape(n) for n in ff.notes) + "</div>"

    doc = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<style>{_CSS}</style></head>
<body><div class="ff">
<h3>{html.escape(ff.entity or "标的")} · Football Field</h3>
<div class="sub">单位：{html.escape(ff.unit)}
{'　基准日：' + html.escape(ff.as_of) if ff.as_of else ''}</div>
<table>{"".join(rows)}
<tr><td></td><td class="track"><div class="axis">{ticks}</div></td><td></td></tr>
</table>
{ref}{cons}{notes}
</div></body></html>"""

    if path:
        Path(path).write_text(doc, encoding="utf-8")
    return doc


def save_json(ff: FootballField, path: str | Path) -> None:
    """存成 JSON —— 换渲染器（Excel 等）时读这个，不用重算。"""
    Path(path).write_text(json.dumps(ff.to_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")
