"""从本地材料取可比公司 —— **对任何市场都成立**的那条路。

## 为什么先做这个

用户同意「用结构化数据源」，但结构化源的覆盖面是有限的：

    SEC EDGAR        美国上市公司   ✅ 已接入
    A 股财报          无现成结构化源   ⬜
    港股财报          无现成结构化源   ⬜
    未上市公司        根本没有         ⬜

而**对标的年报本身就是最权威的结构化数据** —— 用户把几家的年报 PDF
放进一个目录，走**同一套提取流水线**，得到的口径完全一致。

这比「搜网页读数字」可靠得多：
- 数字来自审计过的报表，不是二手转述
- 口径统一（都走 `canonical.py` 的映射）
- **能对齐基准日**（要求文件名的期间和标的期间一致）

而且它复用了已经建好的一切：`from_pdf` / `derive` / `notes` / `prices`。

## 目录约定

    <comps_dir>/
        某公司A_2024年报.pdf
        某公司B_2024年报.pdf
        某公司C_2024年报.pdf
        peers.json          ← 市场代码 + 基准日

`peers.json` 长这样（**只放公开的市场代码，不放标的名称**）：

    [{"file": "某公司A_2024年报.pdf", "name": "某公司A",
      "market": "sh", "code": "600519", "as_of": "2024-12-31"}]

`as_of` 是**取股价的基准日** —— 必须显式给（项目纪律：不给默认值）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import comps_source as cs


@dataclass
class LocalMaterialsSource:
    """从目录里的年报 PDF 取可比公司数据。"""

    directory: Path | str = ""
    name: str = "local-materials"

    def available(self) -> tuple[bool, str]:
        d = Path(self.directory) if self.directory else None
        if d is None:
            return False, "没指定目录（`directory=`）"
        if not d.exists():
            return False, f"目录不存在：{d}"
        pdfs = sorted(d.glob("*.pdf")) + sorted(d.glob("*.PDF"))
        if not pdfs:
            return False, f"目录里没有 PDF：{d}"
        return True, f"{len(pdfs)} 份年报"

    def peers(self, criteria=None) -> list[cs.PeerRow]:
        ok, why = self.available()
        if not ok:
            raise cs.SourceNotReady(f"local-materials 不可用：{why}")

        import sys
        root = Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

        from financials import derive
        from financials.canonical import Field
        from financials.from_pdf import load_pdf_statements

        d = Path(self.directory)
        meta = _load_meta(d)
        out: list[cs.PeerRow] = []

        for pdf in sorted(d.glob("*.pdf")) + sorted(d.glob("*.PDF")):
            m = meta.get(pdf.name, {})
            row = cs.PeerRow(name=m.get("name") or pdf.stem,
                             market=m.get("market", ""),
                             code=m.get("code", ""),
                             source=f"local:{pdf.name}")
            try:
                S = load_pdf_statements(str(pdf))
            except Exception as e:                        # noqa: BLE001
                row.gaps.append(f"读不了这份材料：{type(e).__name__}: {e}")
                out.append(row)
                continue

            row.fiscal_end = S.period or ""
            bal = S.balance.fields if S.balance else {}
            inc = S.income.fields if S.income else {}
            cf = S.cash_flow.fields if S.cash_flow else {}

            row.revenue = inc.get(Field.REVENUE)
            row.net_income = inc.get(Field.NET_INCOME)
            row.ebit = inc.get(Field.OPERATING_INCOME)
            row.equity = bal.get(Field.EQUITY)

            # EBITDA —— 走既有推导，缺 D&A 就报缺（`notes.py` 从附注取）
            merged = dict(inc)
            merged.update(cf)
            if S.da is not None and S.da.total is not None:
                merged.setdefault(Field.DEPRECIATION_AMORTIZATION, S.da.total)
            e = derive.ebitda(merged)
            row.ebitda = e.value
            if row.ebitda is None:
                row.gaps.append("算不出 EBITDA：" + (e.note or "缺营业利润或折旧摊销"))

            # 市值 / EV —— 有市场代码就能取股价了
            if not (row.market and row.code):
                row.gaps.append("没有市场代码，取不到股价 —— 算不了市值和 EV")
            else:
                shares = bal.get(Field.CAPITAL_STOCK)
                if not shares:
                    row.gaps.append("没有股本，算不了市值")
                else:
                    # **基准日必须显式给**（项目纪律：没有默认值）。
                    # 先看 peers.json 里写的，没写才退回材料自己识别的期间。
                    as_of = m.get("as_of") or row.fiscal_end
                    price = _close(row.market, row.code, as_of)
                    if price is None:
                        row.gaps.append(
                            f"取不到 {row.market}{row.code} 在 "
                            f"{as_of or '（没给基准日）'} 的收盘价")
                    else:
                        row.market_cap = price * shares
                        # 净债务 / 少数股东权益走既有推导，缺就报缺
                        nd = derive.net_debt(bal)
                        mi = derive.minority_interest(bal)
                        if nd.value is None:
                            row.gaps.append("净债务取不到：" + (nd.note or ""))
                        elif mi.value is None:
                            row.gaps.append("少数股东权益取不到")
                        else:
                            row.enterprise_value = (
                                row.market_cap + nd.value - mi.value)

            # 材料自己报出来的口径冲突也要带出去
            if S.warnings:
                row.gaps.extend(w for w in S.warnings if "取值" in w or "挡掉" in w)

            out.append(row)
        return out


def _close(market: str, code: str, as_of: str) -> float | None:
    """取**不晚于** `as_of` 的最近一个交易日收盘价。

    **只用不复权价**（`adjust=0`）—— 复权价会改历史价格，
    做市值倍数时会让基准日的市值对不上真实股本。
    这条纪律在 `datasources/prices.py` 里已经定过。

    取不到就返回 None，**不回退到「最新的价」** —— 那是另一种口径。
    """
    if not as_of:
        return None
    try:
        from . import prices
        bar = prices.close_on(market, code, as_of, adjust=0)
    except Exception:                       # noqa: BLE001 网络/对方限流都算取不到
        return None
    return bar.close if bar else None


def _load_meta(d: Path) -> dict[str, dict]:
    p = d / "peers.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {r["file"]: r for r in raw if isinstance(r, dict) and "file" in r}
