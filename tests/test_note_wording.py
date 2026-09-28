"""运行时提示文案也要过口语检查 —— `check_ui.py` 的黑名单**只查 HTML 页面**。

## 为什么要补这一层
`check_ui.py` 的 `COLLOQUIAL` 是给用户看的文案规范（禁「丢材料」「认材料」「出报告」
「认出来了」这类口语）。但它只扫静态 HTML —— 而**引擎生成的提示**（取数依据、反算结论、
口径说明）同样是用户会读到的句子，却从来没被检查过。
实测踩过一次：运行时报文「这行认出来了，但没抽到数字」正好撞上黑名单 ✗。

所以这里把黑名单用在**运行时真正会产生的那批句子**上。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from financials import notes as nt                          # noqa: E402
from financials import statements as stm                    # noqa: E402
from financials.canonical import Field                      # noqa: E402


def _load_colloquial() -> list[str]:
    """从 `tools/check_ui.py` 里取黑名单（**单一来源** —— 不在这里另抄一份）。"""
    spec = importlib.util.spec_from_file_location("_check_ui", ROOT / "tools" / "check_ui.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:                                      # 脚本式入口也不该拦住我们
        pass
    return list(getattr(mod, "COLLOQUIAL", []))


def _set(name: str, rows, fields):
    s = stm.StatementSet(name=name, source="test")
    s.rows = [stm.StatementRow(label=l, value=v, field=None, via="test") for l, v in rows]
    s.fields = fields
    return s


def _all_notes() -> list[str]:
    """把几种典型情形都造出来，收齐**运行时会产生**的那些句子。

    **每个分支都要走到** —— 第一次写这个夹具时我把字段映射取错了行，
    结果"核对自洽"那类句子根本没产生，注入口语词后守卫也不响 ✗：
    一个覆盖不到的守卫，等于没有守卫。
    """
    out: list[str] = []

    # 情形 A：一切自洽（反算一致 / 分项自洽 / 资本开支在界内）
    inc_a = [("营业收入", 1000.0), ("毛利", 455.0), ("其他收入", 165.0),
             ("其他亏损净额", -171.0), ("销售费用", -1345.0), ("管理费用", -1217.0),
             ("研发费用", -243.0), ("营业利润", -2356.0)]
    bal_a = [("应收账款", 30.0), ("存货", 20.0), ("应付账款", 25.0),
             ("流动资产合计", 100.0), ("流动负债合计", 80.0)]
    cf_a = [("购建固定资产、无形资产和其他长期资产支付的现金", -500.0),
            ("投资活动现金流出小计", 1000.0)]
    # 情形 B：反算对不上（走"不一致/无法归因"分支）
    inc_b = [("营业收入", 1000.0), ("毛利", 10.0), ("销售费用", -5.0),
             ("营业利润", -9999.0)]
    # 情形 C：分项超过合计（走"不自洽"分支）
    bal_c = [("应收账款", 9999.0), ("应付账款", 5.0), ("流动负债合计", 1.0)]

    cases = (
        (inc_a, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -2356.0},
         bal_a, {Field.ACCOUNTS_RECEIVABLE: 30.0, Field.INVENTORY: 20.0,
                 Field.ACCOUNTS_PAYABLE: 25.0},
         cf_a, {Field.CAPEX: -500.0}),
        (inc_b, {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: -9999.0},
         [], {}, [], {}),
        ([("营业收入", 1000.0), ("毛利", 455.0), ("销售费用", -10.0), ("营业利润", 445.0)],
         {Field.REVENUE: 1000.0, Field.OPERATING_INCOME: 445.0},
         bal_c, {Field.ACCOUNTS_RECEIVABLE: 9999.0, Field.ACCOUNTS_PAYABLE: 5.0},
         [], {}),
    )
    for inc_rows, inc_fields, bal_rows, bal_fields, cf_rows, cf_fields in cases:
        S = stm.Statements(gaap="", scope="", audited="", period="", unit="千元")
        S.income = _set("income", inc_rows, inc_fields)
        if bal_rows:
            S.balance = _set("balance", bal_rows, bal_fields)
        if cf_rows:
            S.cash_flow = _set("cash_flow", cf_rows, cf_fields)
        S.da = nt.DepreciationAmortisation(components={"固定资产折旧": 9.2}, pages=[72])
        out.extend(S.history_notes().values())
        out.append(S.operating_income_crosscheck())
        out.append(S.nwc_crosscheck())
        out.append(S.capex_crosscheck())
        out.append(S.da.render())
    return [n for n in out if n]


class TestRuntimeNotesAreFormal(unittest.TestCase):
    def test_blacklist_is_not_empty(self):
        self.assertTrue(_load_colloquial(), "没能从 check_ui.py 取到黑名单")

    def test_runtime_notes_have_no_colloquial_words(self):
        block = _load_colloquial()
        for note in _all_notes():
            for w in block:
                self.assertNotIn(w, note, f"运行时提示里出现口语「{w}」：{note[:60]}")


if __name__ == "__main__":
    unittest.main()
