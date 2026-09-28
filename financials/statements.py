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
    #: 这张表的数值列**原名叫什么**，按重要性排序（第 0 个是主数值列）。
    #:
    #: ## 为什么必须记下来（实测踩到）
    #:
    #: 不同表的「另一列」含义完全不同：
    #:
    #:     资产负债表   年初数 / 期末数      ← 年初在前！
    #:     损益表       本月数 / 本年累计
    #:
    #: 照搬「取第一个数值列」会把**去年的数当成今年的**，而且不报错。
    #: 所以列的角色从表头读、主数值列永远放在 `rows[i][2]`，
    #: 同时把原文列名留在这里，让报告能说清「这个数取自哪一列」。
    columns: list[str] = field(default_factory=list)
    #: **同一个字段出现多个不同取值**时的记录（合并 / 母公司口径混在一起等）。
    #: 空表示没有冲突。非空时报告必须显示 —— 这类错不会让勾稽不平。
    conflicts: list[str] = field(default_factory=list)


#: **利润表**的映射率低于这个值，就认为这张表没被读懂。
#:
#: 只对利润表用"映射率"这个判据，另外两张表各有更准的判据：
#:   * 资产负债表 —— 勾稽（资产 = 负债 + 权益）盯着，那比映射率更能说明问题；
#:   * 现金流量表 —— 它的行大多是明细小项，我们不映射也不影响用。
#:     实测 Fitbit 的现金流量表是 24/61（**39%**），但折旧摊销与经营现金流都取到了、
#:     三条勾稽全平 —— 按 50% 一刀切会**误报**。
#:     **一个会喊狼来了的检查，比没有检查更坏。**
MIN_MAPPING_RATE = 0.5

#: 引擎离不了的科目（缺了就算不出估值）—— 这才是"表没读懂"的主判据。
#: 用**科目**判比用**比率**判准：比率低不一定是问题（明细行多而已），
#: 而少一个关键科目，对应的估值方法就是真的跑不起来。
KEY_FIELDS: tuple[tuple[Field, str], ...] = (
    (Field.REVENUE, "营业收入"),
    (Field.OPERATING_INCOME, "营业利润"),
    (Field.DEPRECIATION_AMORTIZATION, "折旧与摊销"),
    (Field.CFO, "经营活动产生的现金流量净额"),
)


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
    #: 金额单位。**空字符串 = 没认出来**，下游必须停下来问人。
    #:
    #: 放在这里是因为装载器**读过正文**（PDF 的文字层、HTML 的表格），
    #: 单位提示就在它读过的那些页里；而调用方拿不到那些页的文本。
    #: 认错单位是 1000 倍级的静默错误，所以宁可留空也不给默认值「元」。
    unit: str = ""
    warnings: list[str] = field(default_factory=list)
    #: 附注里抽出来的折旧摊销（`financials/notes.py`）。
    #: **估值要它** —— 没有 D&A 就算不出 EBITDA，倍数法整条路走不通。
    da: object | None = None

    def mapping_rates(self) -> list[tuple[str, int, int]]:
        """每张表的映射情况：`[(表名, 映射行数, 总行数)]`。"""
        out: list[tuple[str, int, int]] = []
        for name, st in (("资产负债表", self.balance), ("利润表", self.income),
                         ("现金流量表", self.cash_flow)):
            if st is not None:
                out.append((name, sum(1 for r in st.rows if r.field is not None),
                            len(st.rows)))
        return out

    def missing_key_fields(self) -> list[str]:
        """引擎离不了的科目里，哪些**没映射上**。"""
        inc = self.income.fields if self.income else {}
        cf = self.cash_flow.fields if self.cash_flow else {}
        da = self.da_total()
        out = []
        for f, name in KEY_FIELDS:
            if f is Field.DEPRECIATION_AMORTIZATION:
                # D&A 可以在利润表、现金流量表或附注里 —— 口径就一个：`da_total()`
                if da is None:
                    out.append(name)
            elif f is Field.CFO:
                if cf.get(f) is None:
                    out.append(name)
            elif f not in inc:
                out.append(name)
        return out

    def mapping_warnings(self) -> list[str]:
        """「这张表没读懂」的**显式警告** —— 任何行业都用得上。

        ## 为什么必须显式

        实测一份 176 页的保险公司中期报告：利润表 8/76、现金流量表 7/35，
        收入 / 营业利润 / 折旧摊销**全都没映射上**。这时引擎的表现是**安静**的：
        预测参考全是「数据不足」、乘数四个指标全是「推不出」、EBITDA 算不出来 ——
        报告照样出得来，只是没有估值结论。人会以为"这份材料本来就缺数"。

        缺就缺，但要说出来：这是**这张表没被读懂**，不是报表里没有。

        ## 判据用"缺科目"而不是"看比率"（实测纠正过）

        一开始两条都用了，结果 **Fitbit 被误报**：它的现金流量表 24/61（39%）
        低于 50%，可折旧摊销与经营现金流都取到了、三条勾稽全平 —— 那份材料是健康的。
        明细行多不等于没读懂。所以现在：比率只对**利润表**看（它的行都该有科目），
        其余靠"关键科目缺没缺"这个准得多的判据。
        """
        warns: list[str] = []
        for name, mapped, total in self.mapping_rates():
            if name != "利润表" or not total:
                continue
            if mapped / total < MIN_MAPPING_RATE:
                warns.append(
                    f"⚠ 利润表只映射上 {mapped}/{total} 行（{mapped / total:.0%}）"
                    f" —— 低于 {MIN_MAPPING_RATE:.0%}，**这张表很可能没被读懂**"
                    "（科目体系不在覆盖范围内，例如金融业报表）")
        missing = self.missing_key_fields()
        if missing:
            warns.append("→ 关键科目没映射上：" + "、".join(missing) +
                         " —— EBITDA、乘数法基数与预测参考都会缺，"
                         "**报告里的估值结论不可用**")
        return warns

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

        if start is None:
            # **表里没有「净利润」行 = 主表没有调节段**，不是缺数据。
            # H 股常见：主表只列一行「经营活动所用现金净额」，后面挂个附注号，
            # 真正的调节表在附注里（实测某 H 股公司是附注 22(b)）。
            # 报「不适用」而不是「数据不足」—— 后者会让用户去翻一份本来
            # 就不该有这段的表。
            return art.Articulation(
                "净利润 → 经营现金流（间接法）", None, applicable=False,
                note="该现金流量表**没有间接法调节段**：主表只列一行"
                     "「经营活动所用现金净额」并挂附注号，调节表在**附注**里。",
            )
        if end_i is None:
            return art.Articulation(
                "净利润 → 经营现金流（间接法）", None, missing=[Field.CFO],
                note="有调节段，但找不到作为终点的「经营活动现金流量净额」。",
            )
        between = [(r.label, r.value) for r in rows[start + 1:end_i]]
        return art.check_indirect_method(
            between,
            self.cash_flow.fields.get(Field.NET_INCOME),
            self.cash_flow.fields.get(Field.CFO),
        )

    def da_total(self) -> float | None:
        """折旧摊销合计 —— **全项目唯一取数口径**（公开入口）。

        `history()`（历史比率）、`intake`（EBITDA）、`datasources`（可比公司）
        都该从这里取。三处各写一遍的代价实测过：`intake` 那处只看了利润表，
        于是"折旧摊销在现金流量表里"的材料 EBITDA 直接算不出来，乘数法跟着空掉。
        """
        return self._da_total()

    def _da_total(self) -> float | None:
        """折旧摊销合计。

        **它通常在现金流量表的间接法段里，不在利润表。**
        实测：只看利润表取不到，而 D&A 是 EBITDA 的关键组成。

        现金流量表里 D&A 常拆成多行（折旧 / 无形资产摊销 / 加速折旧…），
        所以要**加总所有相关行**，不能只取第一行。

        而且这些行**可能被判成两个不同的科目名**：间接法段里叫 `ID_DA`，
        利润表那套写法叫 `DEPRECIATION_AMORTIZATION` —— 取决于行名长什么样。
        只认一种的代价实测过：同样的材料换一份年报写法，整块取不到。

        **三处来源按可信度排序**（实测踩全过）：
          ① **附注的「现金流量表补充资料」**（`self.da`）—— 那是间接法算出来的
             D&A **总额**，最权威。实测某扫描件的直接法现金表里**根本没有 D&A 行**，
             只有补充资料第 73 页有；抽出来了却没接到这里，于是两个比率一直空着。
          ② 利润表那一行 —— 但可能只是**计入费用的部分**（某白酒公司的「管理费用明细」
             里 `固定资产折旧 6.6 亿` 只占全部 18.9 亿的三分之一），拿它当 D&A 会低估。
          ③ 现金流量表的间接法行 —— 有就用。
        """
        # ① 补充资料（附注）—— 最权威，优先
        if self.da is not None:
            v = getattr(self.da, "total", None)
            if v:
                return float(v)
        # ② 利润表
        if self.income:
            v = self.income.fields.get(Field.DEPRECIATION_AMORTIZATION)
            if v is not None:
                return v
        if not self.cash_flow:
            return None
        # ③ 现金流量表（间接法段）
        vals = [r.value for r in self.cash_flow.rows
                if r.field in (Field.ID_DA, Field.DEPRECIATION_AMORTIZATION)
                and r.value is not None]
        return sum(vals) if vals else None

    # ── 缺数的时候，**说清是缺在哪一步** ──
    # 「数据不足」这四个字是这次被用户问出来的：他分不清是"财报里没有"、
    # "我们没认出来"、还是"认出来了但数字没抽到"—— 三种情况的应对完全不同。
    _ST_OK, _ST_NO_VALUE, _ST_MISSING, _ST_NO_TABLE = "ok", "no-value", "missing", "no-table"

    #: D&A 与收入之比的常识区间 —— 用来**发现量级不对的取数**。
    #:
    #: 实测（某 104 页扫描件）：附注抽到的折旧摊销合计不到收入的 0.1% ✗
    #: —— 一家矿业公司不可能，说明金额取到了但**量级不对**（OCR 把密集数字读花、
    #: 或单位口径不符）。这类错最危险：不报错、不让勾稽不平，一路传进报告。
    #: 区间给得宽（只拦明显离谱的），但足以把这种错拦下来提示人工核对。
    _DA_TO_REV_MIN = 0.001
    _DA_TO_REV_MAX = 0.80

    def _status(self, st, *fields) -> str:
        """某个科目在表里的状态。多给几个字段时，任一有值就算 ok。"""
        if st is None:
            return self._ST_NO_TABLE
        seen_any = False
        for r in st.rows:
            if r.field in fields:
                seen_any = True
                if r.value is not None:
                    return self._ST_OK
        return self._ST_NO_VALUE if seen_any else self._ST_MISSING

    def history_reasons(self) -> dict[str, str]:
        """每个历史比率**为什么**是空的 —— 内容要能直接放到界面上给人看。"""
        inc = self.income
        cf = self.cash_flow
        bal = self.balance
        out: dict[str, str] = {}
        da = self._da_total()

        if da is None:
            st = self._status(cf, Field.ID_DA, Field.DEPRECIATION_AMORTIZATION)
            if st == self._ST_NO_TABLE:
                out["历史折旧摊销占收入比"] = out["历史 EBITDA 率"] = \
                    "现金流量表未识别 —— 折旧摊销通常列示于该表的间接法段"
            elif st == self._ST_MISSING:
                out["历史折旧摊销占收入比"] = out["历史 EBITDA 率"] = \
                    "现金流量表中没有「折旧/摊销」科目行（材料可能未列示，或该写法未覆盖）"
            else:
                out["历史折旧摊销占收入比"] = out["历史 EBITDA 率"] = \
                    "「折旧/摊销」所在行已识别，但未取到数值（扫描件常见）"

        oi = inc.fields.get(Field.OPERATING_INCOME) if inc else None
        if oi is None:
            st = self._status(inc, Field.OPERATING_INCOME)
            why = {"no-table": "利润表未识别",
                   "missing": "利润表中没有「营业利润」科目行",
                   "no-value": "「营业利润」所在行已识别，但未取到数值（扫描件常见）"}[st]
            out["历史 EBITDA 率"] = why

        if cf is None:
            out["历史资本开支占收入比"] = "现金流量表未识别"
        elif self._status(cf, Field.CAPEX) != self._ST_OK:
            st = self._status(cf, Field.CAPEX)
            out["历史资本开支占收入比"] = (
                "现金流量表中没有「购建固定资产、无形资产和其他长期资产支付的现金」科目行"
                if st == self._ST_MISSING else
                "资本开支所在行已识别，但未取到数值（扫描件常见）")
        if bal is None:
            out["历史净营运资本占收入比"] = "资产负债表未识别"
        else:
            ar_st = self._status(bal, Field.ACCOUNTS_RECEIVABLE)
            ap_st = self._status(bal, Field.ACCOUNTS_PAYABLE)
            if ar_st != self._ST_OK or ap_st != self._ST_OK:
                bad = []
                if ar_st != self._ST_OK:
                    bad.append("应收账款" + ("缺少该科目行" if ar_st == self._ST_MISSING
                                          else "该行已识别但未取到数值"))
                if ap_st != self._ST_OK:
                    bad.append("应付账款" + ("缺少该科目行" if ap_st == self._ST_MISSING
                                          else "该行已识别但未取到数值"))
                out["历史净营运资本占收入比"] = (
                    "营运资本需「应收账款 + 存货 − 应付账款」，缺：" + "；".join(bad))
        return out

    # ── 经营利润的反算（用利润表**其他行**把它推出来）─────────────────────────
    #
    # **为什么按行标签找、而不是用科目映射**：这一整套反算的用途，就是检查
    # "映射取到的数对不对" —— 用同一套映射去验，等于**自证清白**：
    # 映射错了照样"验证通过"。所以这里只认**行上的文字**。
    _DERIVE_ADD = (("毛利", ("Gross Profit", "毛利", "毛利润")),
                   ("其他收入", ("Other revenue", "其他收入", "其他业务收入", "其他業務收入")),
                   ("其他损益净额", ("Other net loss", "其他虧損淨額", "其他亏损净额",
                                 "Other net gain", "其他淨收益", "其他净收益")))
    _DERIVE_SUB = (("销售费用", ("Selling and marketing", "銷售及營銷", "销售及营销",
                                 "销售费用", "銷售費用")),
                   ("管理费用", ("General and administration", "一般及行政",
                                 "管理费用", "管理費用")),
                   ("研发费用", ("Research and development", "研發開支", "研发开支",
                                 "研发费用", "研發費用")))

    def _row_in(self, st, patterns: tuple[str, ...]) -> tuple[float, str] | None:
        """在**指定那张表**里按行标签取一行（不走科目映射，理由见 `_DERIVE_ADD`）。"""
        for r in ((st.rows if st else []) or []):
            if r.value is None:
                continue
            lab = r.label or ""
            for p in patterns:
                if p in lab:
                    return r.value, lab
        return None

    def _row_by_label(self, patterns: tuple[str, ...]) -> tuple[float, str] | None:
        """按**行标签**取一行（不走科目映射，见 `_DERIVE_ADD` 的说明）。"""
        return self._row_in(self.income, patterns)

    def derive_operating_income(self) -> tuple[float | None, list[str]]:
        """用利润表其他行反算经营利润。返回 `(值, 用到的行名)`。

        两种印法都要认（**实测两种都见过**）：
          · A 股把费用印成**正数** → 要减
          · 港交所/IFRS 印成**负数**（本身已带符号）→ 直接加
        判据是费用行的符号本身（多数为负 = 已带符号），不写死某一家的格式。
        """
        adds, used = 0.0, []
        for name, pats in self._DERIVE_ADD:
            hit = self._row_by_label(pats)
            if hit:
                adds += hit[0]
                used.append(name)
        subs = [(n, hit) for n, pats in self._DERIVE_SUB
                if (hit := self._row_by_label(pats))]
        gross = self._row_by_label(self._DERIVE_ADD[0][1])
        if gross is None or not subs:
            return None, []
        used += [n for n, _ in subs]
        signed = sum(1 for _, (v, _l) in subs if v < 0)
        if signed * 2 > len(subs):                            # 多数为负 → 已带符号
            total = adds + sum(v for _n, (v, _l) in subs)
        else:                                                 # 正数 → 是绝对额，要减
            total = adds - sum(abs(v) for _n, (v, _l) in subs)
        return total, used

    def _attribute_gap(self, gap: float) -> str:
        """差额与哪一行吻合 —— **差额常常自己指认嫌疑人**。

        实测：第一次反算差 17 个百分点，而 17% 正好等于「其他亏损净额」那一行 →
        立刻知道是我的式子少列了一行，而不是材料有问题。
        三张表都扫：闯祸的行未必在利润表里。
        """
        if not gap:
            return ""
        for st in (self.income, self.cash_flow, self.balance):
            for r in (st.rows if st else []):
                if r.value is None:
                    continue
                if abs(abs(r.value) - gap) <= max(abs(gap) * 0.02, 1.0):
                    return (r.label or "").strip()[:24]
        return ""

    def operating_income_crosscheck(self) -> str:
        """经营利润的反算结论。

        **只报状态，不替人拍板**：两个独立来源一致 → 说一致；
        不一致 → 说差多少、差额像哪一行；归不上任何行 → 明说"未经交叉验证"。
        工具唯一的判断力来源是材料自己的算术，算术对不上时它就没有依据了 ——
        所以绝不静默选一个、绝不取平均、绝不把对不上的那个丢掉。
        """
        inc = self.income.fields if self.income else {}
        op = inc.get(Field.OPERATING_INCOME)
        derived, used = self.derive_operating_income()
        if op is None or derived is None or not used:
            return ""
        op = float(op)
        derived = float(derived)
        rev = inc.get(Field.REVENUE)
        scale = abs(rev) or abs(op) or 1.0
        diff = derived - op
        if abs(diff) <= max(scale * 0.005, 1.0):
            return f"按「{' + '.join(used)}」反算一致 —— 两个来源互相印证"
        who = self._attribute_gap(abs(diff))
        if who:
            return (f"与反算结果不一致（差 {diff / scale:+.2%}，差额与「{who}」吻合）"
                    f"—— 请确认该行是否应计入经营利润")
        return (f"与反算结果不一致（差 {diff / scale:+.2%}，无法归因到某一行）"
                f"—— 该值未经交叉验证，请人工核对")

    #: 净营运资本的「分项 vs 合计」校验用的标签（科目表里没有这两个合计，按行标签取）。
    _CUR_A = ("流动资产合计", "流動資產合計", "Total current assets")
    _CUR_L = ("流动负债合计", "流動負債合計", "Total current liabilities")

    def nwc_crosscheck(self) -> str:
        """净营运资本的「分项 vs 合计」校验 —— 抓三类真会发生的错：

        ① **分项量级读错**（例如小了/大了三个数量级）
        ② **合并表与母公司表串行**（实测某 A 股年报「其他流动资产」一行出现 6 个取值）
        ③ 分项其实没取到，而使用者以为取到了

        「分项 > 合计」是最硬的信号 —— 那是**算术上不可能**的事，任何口径都解释不了
        （不像"对不上"，它没有"口径差异"的余地）。合计取不到时**什么都不说**，不猜。
        """
        bal = self.balance
        hit_a = self._row_in(bal, self._CUR_A)
        hit_l = self._row_in(bal, self._CUR_L)
        cur_a = hit_a[0] if hit_a else None
        cur_l = hit_l[0] if hit_l else None
        inc_b = self.balance.fields if bal else {}
        checks = ((inc_b.get(Field.ACCOUNTS_RECEIVABLE), "应收账款", cur_a),
                  (inc_b.get(Field.INVENTORY), "存货", cur_a),
                  (inc_b.get(Field.ACCOUNTS_PAYABLE), "应付账款", cur_l))
        bad = [f"{name} 大于{'流动资产' if cap == cur_a else '流动负债'}合计"
               for v, name, cap in checks if v is not None and cap and v > cap + abs(cap) * 0.02]
        if bad:
            return ("分项与合计不自洽（" + "；".join(bad) +
                    "）—— 很可能是合并/母公司口径串行，或某个数读错了量级，请核对")
        if cur_a and inc_b.get(Field.ACCOUNTS_RECEIVABLE) is not None:
            return "与「流动资产合计 / 流动负债合计」核对自洽"
        return ""

    def history_notes(self) -> dict[str, str]:
        """算出来了、但口径上要说明一句的（**不能默不作声**）。"""
        bal = self.balance.fields if self.balance else {}
        out: dict[str, str] = {}
        # **量级不对的取数必须自己喊出来。** 实测某扫描件：附注抽到的 D&A 不到收入的
        # 0.1%，而它是一家矿业公司 —— 数字取到了却是错的，而且不会让任何校验不平。
        rev = self.income.fields.get(Field.REVENUE) if self.income else None
        da = self.da_total()
        # **页面内勾稽的结论优先**：恒等式不平说明这一段数字被读错了 ——
        # 这比"量级看起来不对"更硬（它是材料自己的算术在说话）。
        if getattr(self.da, "suspect", False):
            why = getattr(self.da, "note", "") or "附注调节段不平"
            out["历史折旧摊销占收入比"] = f"取数可疑 —— {why}"
            out["历史 EBITDA 率"] = f"取数可疑（折旧摊销）—— {why}"
        elif da and rev:
            ratio = da / rev
            if not (self._DA_TO_REV_MIN <= ratio <= self._DA_TO_REV_MAX):
                out["历史折旧摊销占收入比"] = (
                    "取自附注，但与收入相比明显不符常识（请核对单位或 OCR 取数）")
                out["历史 EBITDA 率"] = (
                    "折旧摊销量级可疑，这一栏同时受影响（请核对）")
        if bal.get(Field.INVENTORY) is None and bal.get(Field.ACCOUNTS_RECEIVABLE) is not None:
            out["历史净营运资本占收入比"] = "未含存货（材料中无此科目）"
        # **净营运资本也摆依据 + 与本表合计对一遍。**
        # 分项超过合计是**算术上不可能**的事（不像"对不上"，它没有口径差异的余地），
        # 能抓到量级读错和合并/母公司串行这两类真会发生的错。
        ar = bal.get(Field.ACCOUNTS_RECEIVABLE)
        nwc_parts: list[str] = []
        unit = self.unit or ""
        if ar is not None and bal.get(Field.ACCOUNTS_PAYABLE) is not None:
            nwc_parts.append(
                f"依据：应收账款 {ar:,.0f}{unit}"
                + (f" + 存货 {bal[Field.INVENTORY]:,.0f}{unit}"
                   if bal.get(Field.INVENTORY) is not None else "")
                + f" − 应付账款 {bal[Field.ACCOUNTS_PAYABLE]:,.0f}{unit}")
        nwc_check = self.nwc_crosscheck()
        if nwc_check:
            nwc_parts.append(nwc_check)
        if nwc_parts:
            joined = "；".join(nwc_parts)
            prev = out.get("历史净营运资本占收入比", "")
            out["历史净营运资本占收入比"] = f"{prev}；{joined}" if prev else joined
        if self.cash_flow and self.cash_flow.fields.get(Field.CAPEX) is not None:
            out["历史资本开支占收入比"] = "按「购建固定资产类支付的现金」口径"

        # **把 EBITDA 率的取数依据摆出来，并自己反算一遍。**
        # 亏得深的公司负 EBITDA 率是真实的（实测某港股 −235%，是真的）——
        # 极端值只有把依据摆出来才可解释；而"反算一致"比"看着像"硬得多。
        # 反算不可得时**什么都不说**（不猜、不噪声）。
        inc = self.income.fields if self.income else {}
        op = inc.get(Field.OPERATING_INCOME)
        parts: list[str] = []
        if op is not None and da is not None:
            unit = self.unit or ""
            parts.append(f"依据：营业利润 {op:,.0f}{unit} + 折旧摊销 {da:,.0f}{unit}")
        check = self.operating_income_crosscheck()
        if check:
            parts.append(check)
        if parts:
            joined = "；".join(parts)
            prev = out.get("历史 EBITDA 率", "")
            out["历史 EBITDA 率"] = f"{prev}；{joined}" if prev else joined
        return out

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
            # **营运资本：应收 + 存货 − 应付，但存货可以是 0。**
            # 原来要求三个科目都在，服务业/没有存货科目的材料会被整块判成"数据不足" ——
            # 那是误伤：应收减应付本身就是营运资本的主干。
            ar = bal.get(Field.ACCOUNTS_RECEIVABLE)
            inv = bal.get(Field.INVENTORY)
            ap = bal.get(Field.ACCOUNTS_PAYABLE)
            if ar is not None and ap is not None:
                out["历史净营运资本占收入比"] = ((ar or 0.0) + (inv or 0.0) - (ap or 0.0)) / rev
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
