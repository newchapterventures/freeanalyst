"""从招股说明书里取**股本结构** —— 估值要用它把企业价值换算成每股价值。

## 为什么（估值链的最后一环）

Football Field 的横轴是**每股价值**，而从 DCF / 倍数法算出来的是**企业价值**
或**股权价值**。换算要两个东西：

    每股价值 = 股权价值 ÷ 总股本
    股权价值 = 企业价值 − 净债务 − 少数股东权益

净债务已经有了（`derive.net_debt`），**总股本一直缺**。招股说明书的
「发行概况」页有：

    发行股数        不超过 217,243,733 股，占发行后总股本比例不低于 10.00%
    每股面值        人民币 1.00 元
    每股发行价格    【】
    发行后总股本    不超过 2,172,437,000 股

## ⚠️ 发行价常常是空的 —— 这不是解析失败

这两份都是**申报稿**（招股说明书「申报稿」），定价还没发生。所以：

    每股发行价格  【】        ← 原样就是空的
    每股发行价格  [ ]        ← 同上

**不许回填、不许默认值。** 报告里要明说「材料未定价」，
Football Field 的「当前价格」基准线只能从**行情数据**取。

## ⚠️ 单位是混的

同一句话里既有 `217,243,733 股`（绝对数），也有 `2,000 万股`（万字头）。
都要认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ingest import pdf as ip

#: 「发行概况」页的标志
_PROFILE_MARKERS = ("发行概况", "本次发行概况", "发行股票类型",
                    "每股发行价格", "发行股数")

#: 空值占位 —— 申报稿里价格还没定，原样就是这样
_BLANK = ("【】", "[]", "[ ]", "【 】", "－", "—", "待定", "未确定")

#: 「不超过 2,000 万股」这种 —— 数字 + 可选「万」 + 「股」
_AMOUNT_SHARES = r"([\d,]+(?:\.\d+)?)\s*(万)?\s*股"


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


@dataclass
class ShareStructure:
    """股本结构。**每项都要能说清取自哪一页哪一句话。**"""

    shares_issued: float | None = None      # 本次发行股数
    shares_after: float | None = None       # 发行后总股本
    shares_before: float | None = None      # 发行前总股本（有则取）
    par_value: float | None = None          # 每股面值
    offer_price: float | None = None        # 每股发行价格（申报稿常为空）
    pages: list[int] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        out = []
        if self.pages:
            out.append(f"  取自第 {'、'.join(str(p) for p in self.pages)} 页"
                       f"（发行概况）")
        for label, v in (("本次发行股数", self.shares_issued),
                         ("发行后总股本", self.shares_after),
                         ("发行前总股本", self.shares_before),
                         ("每股面值", self.par_value)):
            if v is not None:
                out.append(f"     {label:12} {v:,.0f}" +
                           ("元/股" if label == "每股面值" else "股"))
        if self.offer_price is not None:
            out.append(f"     {'每股发行价格':12} {self.offer_price:,.2f}元")
        for n in self.notes:
            out.append(f"  ⚠ {n}")
        return "\n".join(out) if out else "  股本结构：**未取到**"


def find_profile_pages(doc: ip.PdfDocument) -> list[int]:
    """哪几页是「发行概况」。"""
    out = []
    for pg in doc.pages:
        t = pg.text
        if sum(1 for m in _PROFILE_MARKERS if m in t) >= 2:
            out.append(pg.number)
    return out


def _find_amount_near(text: str, label: str, window: int = 70) -> str | None:
    """在标签**附近**找「不超过 N 股 / N 万股」。

    ## 两个坑（都是实测踩出来的）

    **一、标签可能排在数值后面。** 某招股书的文字层是：

        发行股票类型 人民币普通股(A股) 不超过 217,243,733 股,占发行后总股本
        比例不低于发行股数 10.00%。…发行后总股本不超过2,172,437,000股

    「发行股数」出现在一句说明里，真值 217,243,733 在它**前面**。
    窗口开到 120 字时，正则跨过 `10.00%` 抓到**下一个字段**的
    `2,172,437,000股` —— 发行股数被当成 21.7 亿、发行后总股本也是 21.7 亿、
    发行前股本被推成 0。**三个数全错，而且看起来都挺像样。**

    **二、说明句里的标签要跳过。** `占发行后总股本的比例不低于 25%` 里也有
    「发行后总股本」，紧挨着的数字却是**发行股数**。于是发行后总股本
    被读成了发行股数（实测某教育公司：8,000 万股读成 2,000 万股）。

    所以：**逐个出现位置试**，跳过前面带「占 / 比例」的说明性提及
    （`占发行后总股本的比例` 这种），并且优先取紧跟「不超过」的那个数。

    ## 怎么区分「字段」和「说明句里的提及」

    看**标签后面**跟的是什么，不看前面：

        占发行后总股本**的比例**不低于 25%     ← 后面跟「的」→ 说明句，跳过
        比例不低于发行股数 **10.00%**          ← 后面跟百分比 → 是真字段（值在它前面）

    试过看前面几个字（`占` / `比例` / `不低于`），分不开 ——
    `比例不低于发行股数` 里也有「比例」。
    """
    for m0 in re.finditer(re.escape(label), text):
        i = m0.start()
        after = text[i + len(label):i + len(label) + 4]
        if after.startswith(("的", "所占", "比例")):
            continue                       # `占 X 的比例` / `占 X 比例…` 这类说明句
        seg = text[max(0, i - window):i + window]
        hits = list(re.finditer(_AMOUNT_SHARES, seg))
        if not hits:
            continue
        # 「不超过 N 股」优先 —— 招股书里发行股数一律这么写
        for m in hits:
            if "不超过" in seg[max(0, m.start() - 12):m.start()]:
                return m.group(0)
        return hits[0].group(0)
    return None


def _to_shares(s: str) -> float | None:
    m = re.match(_AMOUNT_SHARES, s)
    if not m:
        return None
    v = _num(m.group(1))
    if v is None:
        return None
    return v * 10000 if m.group(2) else v


def extract_shares(doc: ip.PdfDocument) -> ShareStructure:
    """从招股说明书里取股本结构。"""
    out = ShareStructure()
    pages = find_profile_pages(doc)
    if not pages:
        out.notes.append("找不到「发行概况」页 —— 不是招股说明书？")
        return out
    out.pages = pages[:3]
    by_number = {pg.number: pg for pg in doc.pages}
    priced_blank = False

    for n in out.pages:
        text = " ".join(by_number[n].text.split())

        # 发行股数：优先「本次拟公开发行股份数量」，其次「发行股数」
        if out.shares_issued is None:
            for key in ("本次拟公开发行股份数量", "发行股数", "本次发行股数"):
                if key not in text:
                    continue
                hit = _find_amount_near(text, key)
                v = _to_shares(hit) if hit else None
                if v:
                    out.shares_issued = v
                    out.evidence.append(f"第{n}页 {key} = {hit}")
                    break

        # 发行后总股本
        if out.shares_after is None and "发行后总股本" in text:
            hit = _find_amount_near(text, "发行后总股本")
            v = _to_shares(hit) if hit else None
            if v:
                out.shares_after = v
                out.evidence.append(f"第{n}页 发行后总股本 = {hit}")

        # 每股面值
        if out.par_value is None and "每股面值" in text:
            i = text.find("每股面值")
            m = re.search(r"([\d.]+)\s*元", text[i:i + 60])
            if m:
                out.par_value = _num(m.group(1))
                out.evidence.append(f"第{n}页 每股面值 = {m.group(0)}")

        # 每股发行价格 —— **空值要认出来并说明，不能当解析失败**
        if out.offer_price is None and not priced_blank and "每股发行价格" in text:
            i = text.find("每股发行价格")
            seg = text[i:i + 40]
            m = re.search(r"([\d.]+)\s*元", seg)
            if m:
                out.offer_price = _num(m.group(1))
                out.evidence.append(f"第{n}页 每股发行价格 = {m.group(0)}")
            elif any(b in seg for b in _BLANK):
                priced_blank = True
                out.notes.append(
                    "**材料未定价**：发行价格原样就是空的（申报稿阶段还没定价）。"
                    "Football Field 的「当前价格」基准线只能从行情数据取。")

    # **发行前股本 = 发行后 − 发行** —— 但只在两个数都有时才推，且标明是推算
    if out.shares_before is None and out.shares_after and out.shares_issued:
        out.shares_before = out.shares_after - out.shares_issued
        out.notes.append(
            f"发行前股本按「发行后 − 发行」**推算**得 {out.shares_before:,.0f} 股"
            "（材料未直接给出）")

    if out.shares_after is None and out.shares_before and out.shares_issued:
        out.shares_after = out.shares_before + out.shares_issued
        out.notes.append(f"发行后股本按「发行前 + 发行」**推算**得 {out.shares_after:,.0f} 股")

    return out
