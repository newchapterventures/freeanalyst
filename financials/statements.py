"""把解析出的三张表组装成一个可用的「报表集」。

## 一条必须守住的界线：事实自动填，假设绝不自动填

从三张表能推出的东西分两类：

**事实类** —— 某个时点确实存在的数
    base_revenue（上一年实际收入）、net_debt、minority_interest

**假设类** —— 对未来的判断
    ebitda_margin（预测期）、收入增长率、资本开支占收入比

事实类可以自动填；**假设类只报历史参考值，让用户自己定。**

理由：把历史 EBITDA 率自动填成预测假设，会得到一个
「有出处、但出处和历史绑死」的数 —— 用户以为那是自己的判断，
其实是引擎替他判断的。**这正是这个产品要避免的事。**

历史参考值仍然要报（「近三年 EBITDA 率 12.4% / 13.1% / 14.0%」），
但要以"参考"的身份出现在报告里，不是以"输入"的身份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import articulation as art
from . import canonical as cn
from . import derive as dv
from .canonical import Field


@dataclass
class StatementRow:
    label: str
    value: float | None
    field: Field | None
    via: str


@dataclass
class StatementSet:
    """一张报表抽出来的东西 + 它的来源信息。"""

    name: str
    source: str
    unit: str = ""
    rows: list[StatementRow] = field(default_factory=list)
    fields: dict[Field, float] = field(default_factory=dict)
    n_tables: int = 0


@dataclass
class Statements:
    """三张表 + 口径声明。"""

    balance: StatementSet | None = None
    income: StatementSet | None = None
    cash_flow: StatementSet | None = None
    #: §4.1–4.4 的四项申报
    gaap: str = ""
    scope: str = ""
    audited: str = ""
    period: str = ""
    warnings: list[str] = field(default_factory=list)

    def checks(self) -> list[art.Articulation]:
        out: list[art.Articulation] = []
        if self.balance:
            bal = self.balance.fields
            if art.is_net_asset_presentation(bal):
                # **IFRS 净资产列报式**：没有「资产总计」「负债合计」行。
                # 报「不适用」而不是「数据不足」—— 不是漏了数据，是格式不同。
                out.append(art.Articulation(
                    "资产 = 负债 + 所有者权益", None, applicable=False,
                    note="该表用 **IFRS 净资产列报式**（H 股常见）：没有"
                         "「资产总计」「负债合计」行，本项不适用。见下一条等价校验。",
                ))
                out.append(art.check_net_assets(bal))
            else:
                out.append(art.check_balance(bal))
        if self.cash_flow:
            cf = self.cash_flow
            end = self._cash_begin_end()
            out.append(art.check_cash_rollforward(cf.fields, end[0], end[1]))
        if self.cash_flow and self.income:
            out.append(self._indirect())
        return out

    def _cash_begin_end(self) -> tuple[float | None, float | None]:
        """期初期末现金。

        **优先看现金流量表的行名**（那里有「期初/期末」的区分），
        资产负债表的「货币资金」是期末余额，可以给期末兜底。
        """
        assert self.cash_flow is not None
        f = self.cash_flow.fields
        begin = f.get(Field.CASH_BEGIN)
        end = f.get(Field.CASH_END)
        if end is None and self.balance:
            end = self.balance.fields.get(Field.CASH)
        return begin, end

    def _indirect(self) -> art.Articulation:
        """间接法：净利润行与经营现金流行之间的行全部加总。

        **先判方法适用性。** A 股现金流量表是直接法编的，没有那段调节 ——
        硬跑会报「定位不到锚点」，看着像映射漏了，其实不适用。
        """
        assert self.cash_flow is not None and self.income is not None
        rows = self.cash_flow.rows
        pairs = [(r.label, r.value) for r in rows]
        if art.is_direct_method(pairs):
            return art.Articulation(
                "净利润 → 经营现金流（间接法）", None, applicable=False,
                note="该现金流量表用**直接法**编（A 股常见），"
                     "没有这段调节 —— 间接法调节在附注里。",
            )
        start = end_i = None
        for i, r in enumerate(rows):
            if r.field == Field.NET_INCOME and start is None:
                start = i
            if r.field == Field.CFO and start is not None:
                end_i = i
                break
        if start is None or end_i is None:
            return art.Articulation("净利润 → 经营现金流（间接法）", None,
                                    missing=[Field.NET_INCOME, Field.CFO])
        between = [(r.label, r.value) for r in rows[start + 1:end_i]]
        return art.check_indirect_method(
            between,
            self.cash_flow.fields.get(Field.NET_INCOME),
            self.cash_flow.fields.get(Field.CFO),
        )

    def _da_total(self) -> float | None:
        """折旧摊销合计。

        **它通常在现金流量表的间接法段里，不在利润表。**
        实测：只看利润表取不到，而 D&A 是 EBITDA 的关键组成。

        现金流量表里 D&A 常拆成多行（折旧 / 无形资产摊销 / 加速折旧…），
        所以要**加总所有映射到 `ID_DA` 的行**，不能只取第一行。
        """
        if self.income:
            v = self.income.fields.get(Field.DEPRECIATION_AMORTIZATION)
            if v is not None:
                return v
        if not self.cash_flow:
            return None
        vals = [r.value for r in self.cash_flow.rows
                if r.field == Field.ID_DA and r.value is not None]
        return sum(vals) if vals else None

    def history(self) -> dict[str, float | None]:
        """历史比率参考值 —— **不是输入，是参考资料**。"""
        inc = self.income.fields if self.income else {}
        cf = self.cash_flow.fields if self.cash_flow else {}
        bal = self.balance.fields if self.balance else {}

        rev = inc.get(Field.REVENUE)
        out: dict[str, float | None] = {}
        if rev:
            da = self._da_total()
            capex = cf.get(Field.CAPEX)
            oi = inc.get(Field.OPERATING_INCOME)
            out["历史 EBITDA 率"] = ((oi + da) / rev
                                   if (oi is not None and da is not None) else None)
            out["历史折旧摊销占收入比"] = da / rev if da is not None else None
            # **资本开支取绝对值。**
            # 现金流量表里「购建固定资产支付的现金」记的是现金**流出**（负数），
            # 而 DCF 的 capex_pct_revenue 期望一个正数占比再去做减法。
            # 直接给负数会让人以为资本开支为负 —— 那是另一个意思。
            out["历史资本开支占收入比"] = abs(capex) / rev if capex is not None else None
            nwc_parts = [bal.get(Field.ACCOUNTS_RECEIVABLE),
                         bal.get(Field.INVENTORY),
                         bal.get(Field.ACCOUNTS_PAYABLE)]
            if all(v is not None for v in nwc_parts):
                ar = nwc_parts[0] or 0.0
                inv = nwc_parts[1] or 0.0
                ap = nwc_parts[2] or 0.0
                out["历史净营运资本占收入比"] = (ar + inv - ap) / rev
            else:
                out["历史净营运资本占收入比"] = None
        out["历史实际收入"] = rev
        return out

    def facts(self) -> dict[str, dv.Derived]:
        """事实类口径 —— 自动填进 DCF 的部分。"""
        bal = self.balance.fields if self.balance else {}
        inc = self.income.fields if self.income else {}
        out: dict[str, dv.Derived] = {
            "net_debt": dv.net_debt(bal),
            "minority_interest": dv.minority_interest(bal),
        }
        rev = inc.get(Field.REVENUE)
        if rev is not None:
            out["base_revenue"] = dv.Derived(
                "基期营业收入", rev,
                parts=[(Field.REVENUE.value, rev)],
                note="最近一个实际年度",
            )
        return out


def load_one(path: Path, name: str, unit: str = "") -> StatementSet:
    """读一份表（.htm/.html 走 HTML 抽取，其余按纯文本）。"""
    from ingest import html as ih

    doc = ih.extract_html(path)
    st = StatementSet(name=name, source=str(path), unit=unit,
                      n_tables=len(doc.tables))
    if not doc.tables:
        return st
    t = doc.tables[0]
    for r in t.rows:
        if not r.cells:
            continue
        vals = r.numeric_cells()
        v = vals[0] if vals else None
        f, via = cn.identify(r.label, r.xbrl_tag)
        st.rows.append(StatementRow(r.label, v, f, via))
        if f and f not in st.fields and v is not None:
            st.fields[f] = v
    return st
