"""A 股财务数据源（`datasources/cn_financials.py`）测试 —— 全程不联网。

## 这个文件锁住的几条纪律

**① 行业模板要看资产负债表，不能看利润表。**

实测：通用利润表 `GINCOME` **对银行和券商一样有数据**（601398 / 600030 都通），
所以"先试通用、通则算通用"会把银行和券商全判成通用，专用模板永远轮不到。
判据是资产负债表（四套互不重叠）。这条专门有一个用例钉住。

**② 缺就是缺 —— 不许拿 0 填、不许回落到别的报告期。**

"取不到"和"值是 0"、"取不到这一期"和"拿最近一期顶上"，
在数字上分不出来，但在结论上是两回事。

**③ 网络失败 ≠ 这家没有数据。**

东财会间歇性拒连（掐连接、不给响应）。静默返回 None 会让可比集合
悄悄少几家公司，所以失败必须抛错、并把三种可能讲清楚。

## 联网的部分

需要真实请求的回归用例标了 skipUnless，要 `FREEANALYST_NET_TESTS=1`。
其余全部打桩；缓存目录也指到临时目录，不碰本机真实文件。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import net  # noqa: E402
from datasources import cn_financials as cn  # noqa: E402

#: 东财那行的样子（日期带时分秒，字段是 JSON 数字）
_PERIOD = "2026-06-30 00:00:00"


def income_row(**over) -> dict:
    row = {
        "REPORT_DATE": _PERIOD,
        "SECUCODE": "600519.SH",
        "TOTAL_OPERATE_INCOME": 92278072083.21,
        "OPERATE_INCOME": 90703260964.48,
        "PARENT_NETPROFIT": 44516880421.86,
        "OPERATE_PROFIT": 61411291686.27,
    }
    row.update(over)
    return row


def balance_row(**over) -> dict:
    row = {
        "REPORT_DATE": _PERIOD,
        "SECUCODE": "600519.SH",
        "TOTAL_EQUITY": 262096352174.36,
        "TOTAL_PARENT_EQUITY": 251253594419.5,
    }
    row.update(over)
    return row


def payload(rows: list[dict]) -> bytes:
    return json.dumps({"result": {"data": rows}, "success": True},
                      ensure_ascii=False).encode("utf-8")


def empty_payload() -> bytes:
    """实测的空响应长这样（HTTP 200）。"""
    return json.dumps({"result": None, "success": False,
                       "message": "返回数据为空"}, ensure_ascii=False).encode("utf-8")


class _StubTables:
    """把**唯一联网函数** `_fetch_rows` 换掉。

    只换最底层 —— 行业探测、字段挑选、报告期归位这些真实逻辑仍然会被执行。
    如果换成 patch `fetch`，测的就是假函数而不是真逻辑了。

    `tables` 的值可以是「行列表」或「函数(report_date) → 行列表」。
    """

    def __init__(self, tables: dict):
        self.tables = tables
        self.calls: list[tuple[str, str | None]] = []

    def __enter__(self):
        self._orig = cn._fetch_rows
        outer = self

        def fake(secu, suffix, report_date=None, use_cache=True):
            outer.calls.append((suffix, report_date))
            spec = outer.tables.get(suffix, [])
            rows = spec(report_date) if callable(spec) else spec
            return list(rows)

        cn._fetch_rows = fake
        return self

    def __exit__(self, *a):
        cn._fetch_rows = self._orig

    def suffixes(self) -> list[str]:
        return [c[0] for c in self.calls]

    def probes(self) -> list[tuple[str, str | None]]:
        """探测调用 = 不带报告期的那种。"""
        return [c for c in self.calls if c[1] is None]


class _IsolatedCache:
    """缓存指到临时目录，并把限速关掉（否则每个用例白等 0.4 秒）。"""

    def __enter__(self):
        self._dir, self._interval = cn.CACHE_DIR, cn._MIN_INTERVAL
        self._last = list(cn._last_call)
        self._tmp = Path(tempfile.mkdtemp(prefix="freeanalyst-cn-fin-"))
        cn.CACHE_DIR = self._tmp
        cn._MIN_INTERVAL = 0.0
        return self

    def __exit__(self, *a):
        cn.CACHE_DIR, cn._MIN_INTERVAL = self._dir, self._interval
        cn._last_call[:] = self._last


def _all_values(result: dict) -> list:
    return [v["value"] for v in result["values"].values()]


# ---------------------------------------------------------------------------
# 代码 / 报告期的格式
# ---------------------------------------------------------------------------

class TestSecucode(unittest.TestCase):

    def test_measured_prefixes(self):
        """只补**实测过**的首位映射。"""
        self.assertEqual(cn.secucode("600519"), "600519.SH")
        self.assertEqual(cn.secucode("601398"), "601398.SH")
        self.assertEqual(cn.secucode("000001"), "000001.SZ")
        self.assertEqual(cn.secucode("920819"), "920819.BJ")

    def test_explicit_suffix_is_kept(self):
        self.assertEqual(cn.secucode("600519.SH"), "600519.SH")
        self.assertEqual(cn.secucode("300750.sz"), "300750.SZ")

    def test_unknown_prefix_asks_for_explicit_suffix(self):
        """创业板 / 科创板不猜 —— 猜错会取到另一个市场的数据，而且不报错。"""
        with self.assertRaises(cn.CnFinancialsError) as cm:
            cn.secucode("300750")
        msg = str(cm.exception)
        self.assertIn("300750.SZ", msg)
        self.assertIn("300750.SH", msg)

    def test_bad_input_raises(self):
        """内部字符必须全是数字（首尾空白会被去掉 —— 从表格里复制常带）。"""
        for bad in ("", "   ", "60051", "6005199", "abcdef", "6005 19", "600519.XX.1"):
            with self.subTest(bad=bad):
                with self.assertRaises(cn.CnFinancialsError):
                    cn.secucode(bad)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(cn.secucode("  600519  "), "600519.SH")

    def test_bad_suffix_raises(self):
        with self.assertRaises(cn.CnFinancialsError) as cm:
            cn.secucode("600519.XX")
        self.assertIn("SH", str(cm.exception))


class TestReportDateFormat(unittest.TestCase):

    def test_accepts_iso_date(self):
        self.assertEqual(cn.normalize_report_date("2026-06-30"), "2026-06-30")

    def test_rejects_other_formats(self):
        """写错格式会**静默返回空**，而空会被读成「这家没有这一期」—— 所以必须拦住。"""
        for bad in ("2026/06/30", "20260630", "2026-6-3", "", "30-06-2026"):
            with self.subTest(bad=bad):
                with self.assertRaises(cn.CnFinancialsError) as cm:
                    cn.normalize_report_date(bad)
                self.assertIn("YYYY-MM-DD", str(cm.exception))

    def test_rejects_impossible_dates(self):
        """`2026-13-01` 格式对、日子不对 —— 接口同样会静默当成「没有数据」。"""
        for bad in ("2026-13-01", "2026-02-30", "2026-00-10"):
            with self.subTest(bad=bad):
                with self.assertRaises(cn.CnFinancialsError) as cm:
                    cn.normalize_report_date(bad)
                self.assertIn("不是一个真实日期", str(cm.exception))

    def test_fetch_validates_before_going_out(self):
        with _StubTables({}) as stub:
            with self.assertRaises(cn.CnFinancialsError):
                cn.fetch("600519", "2026/06/30")
        self.assertEqual(stub.calls, [], "格式不对就不该发起任何请求")


# ---------------------------------------------------------------------------
# 行业模板
# ---------------------------------------------------------------------------

class TestIndustryDetection(unittest.TestCase):

    def test_general_company(self):
        with _StubTables({"GBALANCE": [balance_row()],
                          "GINCOME": [income_row()]}) as stub:
            out = cn.fetch("600519", "2026-06-30")
        self.assertEqual(out["industry"], "通用")
        self.assertEqual(out["templates"], {"income": "GINCOME", "balance": "GBALANCE"})
        # 第一个探测就命中，不该多试
        self.assertEqual(stub.probes(), [("GBALANCE", None)])

    def test_bank(self):
        with _StubTables({"GBALANCE": [], "BBALANCE": [balance_row()],
                          "BINCOME": [income_row()]}) as stub:
            out = cn.fetch("601398", "2026-06-30")
        self.assertEqual(out["industry"], "银行")
        self.assertEqual(out["templates"], {"income": "BINCOME", "balance": "BBALANCE"})
        self.assertEqual(stub.probes(), [("GBALANCE", None), ("BBALANCE", None)])

    def test_securities(self):
        with _StubTables({"GBALANCE": [], "BBALANCE": [], "IBALANCE": [],
                          "SBALANCE": [balance_row()], "SINCOME": [income_row()]}) as stub:
            out = cn.fetch("600030", "2026-06-30")
        self.assertEqual(out["industry"], "证券")
        self.assertEqual(out["templates"], {"income": "SINCOME", "balance": "SBALANCE"})
        self.assertEqual(stub.probes(),
                         [("GBALANCE", None), ("BBALANCE", None),
                          ("IBALANCE", None), ("SBALANCE", None)])

    def test_insurance(self):
        with _StubTables({"GBALANCE": [], "BBALANCE": [], "IBALANCE": [balance_row()],
                          "IINCOME": [income_row()]}) as stub:
            out = cn.fetch("601601", "2026-06-30")
        self.assertEqual(out["industry"], "保险")
        self.assertEqual(stub.probes(),
                         [("GBALANCE", None), ("BBALANCE", None), ("IBALANCE", None)])

    def test_general_income_table_does_not_decide_industry(self):
        """**本模块实测补的纪律。**

        通用利润表对银行一样有数据（601398 实测通），所以"通用利润表有数据"
        不能当成"这是通用行业" —— 那会让银行/券商永远用通用模板。
        判据只能是资产负债表。

        这里把通用利润表塞一个**明显不同的数**：如果它被用来取数，结果会露馅。
        """
        with _StubTables({
            "GINCOME": [income_row(TOTAL_OPERATE_INCOME=999.0)],      # ← 障眼法
            "GBALANCE": [],                                          # ← 通用资产负债表没有
            "BBALANCE": [balance_row()],
            "BINCOME": [income_row(TOTAL_OPERATE_INCOME=None,
                                   OPERATE_INCOME=465859000000.0)],
        }) as stub:
            out = cn.fetch("601398", "2026-06-30")
        self.assertEqual(out["industry"], "银行")
        self.assertEqual(out["templates"]["income"], "BINCOME")
        # 取数只用银行那一套，通用利润表**一次都没被用来取数**
        self.assertEqual([c for c in stub.calls if c[1] is not None],
                         [("BINCOME", "2026-06-30"), ("BBALANCE", "2026-06-30")])
        self.assertEqual(out["values"]["营业收入"]["value"], 465859000000.0)
        self.assertEqual(out["values"]["营业收入"]["field"], "OPERATE_INCOME")

    def test_explicit_industry_skips_probing(self):
        with _StubTables({"BBALANCE": [balance_row()], "BINCOME": [income_row()]}) as stub:
            out = cn.fetch("601398", "2026-06-30", industry="银行")
        self.assertEqual(out["industry"], "银行")
        self.assertEqual(stub.probes(), [], "显式指定时不该再探测")

    def test_unknown_industry_raises(self):
        with self.assertRaises(cn.CnFinancialsError) as cm:
            cn.fetch("600519", industry="房地产")
        self.assertIn("银行", str(cm.exception))


# ---------------------------------------------------------------------------
# 取值：字段名、口径、缺
# ---------------------------------------------------------------------------

class TestConceptExtraction(unittest.TestCase):

    def _run(self, **tables):
        with _StubTables({"GBALANCE": [balance_row()],
                          "GINCOME": [income_row()], **tables}):
            return cn.fetch("600519", "2026-06-30")

    def test_values_carry_field_and_period(self):
        out = self._run()
        rev = out["values"]["营业收入"]
        self.assertAlmostEqual(rev["value"], 92278072083.21)
        self.assertEqual(rev["field"], "TOTAL_OPERATE_INCOME")
        self.assertEqual(rev["report_date"], "2026-06-30")
        self.assertEqual(rev["table"], "GINCOME")
        self.assertEqual(rev["unit"], "元")

    def test_all_required_concepts_present(self):
        out = self._run()
        for key in cn.REQUIRED:
            self.assertIn(key, out["values"])
        self.assertEqual(out["report_date"], "2026-06-30")
        self.assertEqual(out["requested_report_date"], "2026-06-30")

    def test_falls_back_to_operate_income_and_says_so(self):
        """券商/保险的专用表里没有营业总收入 —— 回落到营业收入，并**记下用的是哪个**。"""
        out = self._run(GINCOME=[income_row(TOTAL_OPERATE_INCOME=None)])
        rev = out["values"]["营业收入"]
        self.assertAlmostEqual(rev["value"], 90703260964.48)
        self.assertEqual(rev["field"], "OPERATE_INCOME")

    def test_none_value_falls_through_to_next_candidate(self):
        out = self._run(GINCOME=[income_row(TOTAL_OPERATE_INCOME=None,
                                            OPERATE_INCOME=123.0)])
        self.assertEqual(out["values"]["营业收入"]["value"], 123.0)

    def test_zero_is_a_real_value(self):
        """**0 不是「缺」。** 拿 0 当缺会把真数字丢掉。"""
        out = self._run(GINCOME=[income_row(OPERATE_PROFIT=0)])
        self.assertEqual(out["values"]["营业利润"]["value"], 0.0)
        self.assertNotIn("营业利润", " ".join(out["gaps"]))

    def test_missing_field_is_none_with_reason(self):
        row = income_row()
        del row["OPERATE_PROFIT"]
        out = self._run(GINCOME=[row])
        self.assertIsNone(out["values"]["营业利润"]["value"])
        self.assertIsNone(out["values"]["营业利润"]["field"])
        self.assertTrue(any("营业利润" in g for g in out["gaps"]))
        self.assertIn("OPERATE_PROFIT", out["values"]["营业利润"]["note"])

    def test_string_numbers_are_read(self):
        out = self._run(GINCOME=[income_row(OPERATE_PROFIT="6,141.13")])
        self.assertAlmostEqual(out["values"]["营业利润"]["value"], 6141.13)

    def test_unparsable_value_is_missing_not_zero(self):
        out = self._run(GINCOME=[income_row(OPERATE_PROFIT="--")])
        self.assertIsNone(out["values"]["营业利润"]["value"])
        self.assertIn("不是数字", out["values"]["营业利润"]["note"])

    def test_equity_uses_parent_not_total(self):
        """归母权益和权益合计不是一个数（实测 601398 差 288.5 亿 = 少数股东权益）。"""
        out = self._run()
        self.assertAlmostEqual(out["values"]["所有者权益(归母)"]["value"],
                               251253594419.5)
        self.assertEqual(out["values"]["所有者权益(归母)"]["field"],
                         "TOTAL_PARENT_EQUITY")
        self.assertAlmostEqual(out["values"]["所有者权益合计"]["value"],
                               262096352174.36)

    def test_missing_is_never_zero(self):
        """全局纪律：缺的值必须是 None，不能是 0。"""
        with _StubTables({"GBALANCE": [], "BBALANCE": [], "IBALANCE": [],
                          "SBALANCE": [], "GINCOME": [], "BINCOME": [],
                          "IINCOME": [], "SINCOME": []}):
            out = cn.fetch("600519", "2026-06-30")
        self.assertTrue(all(v is None for v in _all_values(out)))
        self.assertGreaterEqual(len(out["gaps"]), len(cn.REQUIRED))

    def test_period_caveat_is_always_attached(self):
        """累计口径这条必须跟着结果走 —— 中报的数不是单季。"""
        out = self._run()
        self.assertIn(cn.PERIOD_NOTE, out["notes"])
        self.assertIn("累计", cn.PERIOD_NOTE)
        self.assertIn("元", cn.PERIOD_NOTE)


# ---------------------------------------------------------------------------
# 报告期
# ---------------------------------------------------------------------------

class TestReportPeriod(unittest.TestCase):

    def test_requested_period_goes_to_both_tables(self):
        with _StubTables({"GBALANCE": [balance_row()],
                          "GINCOME": [income_row()]}) as stub:
            cn.fetch("600519", "2024-12-31")
        self.assertEqual([c for c in stub.calls if c[1] is not None],
                         [("GINCOME", "2024-12-31"), ("GBALANCE", "2024-12-31")])

    def test_latest_period_when_none(self):
        with _StubTables({"GBALANCE": [balance_row()],
                          "GINCOME": [income_row()]}) as stub:
            out = cn.fetch("600519")
        self.assertIsNone(out["requested_report_date"])
        self.assertEqual(out["report_date"], "2026-06-30")
        self.assertTrue(all(c[1] is None for c in stub.calls),
                        "不带报告期 = 让接口给最新一期")

    def test_does_not_fall_back_to_another_period(self):
        """**核心纪律**：要的那一期没有，就返回空，不许拿最近一期顶上。

        顶上去的话数字看着完全正常，而它属于另一个期间。
        """
        def by_date(report_date):
            return [] if report_date == "2019-03-31" else [income_row()]

        with _StubTables({"GBALANCE": [], "GINCOME": by_date}):
            out = cn.fetch("600519", "2019-03-31", industry="通用")
        self.assertTrue(all(v is None for v in _all_values(out)))
        self.assertEqual(out["report_date"], None)
        self.assertIn("2019-03-31", " ".join(out["notes"]))

    def test_period_mismatch_is_flagged(self):
        """回来的不是要的那一期 —— 必须说出来，不能安静地换期。"""
        with _StubTables({"GBALANCE": [balance_row(REPORT_DATE="2025-12-31 00:00:00")],
                          "GINCOME": [income_row(REPORT_DATE="2025-12-31 00:00:00")]}) as _:
            out = cn.fetch("600519", "2026-06-30")
        self.assertEqual(out["report_date"], "2025-12-31")
        self.assertTrue(any("⚠️" in n for n in out["notes"]))

    def test_table_period_mismatch_is_flagged(self):
        with _StubTables({"GBALANCE": [balance_row(REPORT_DATE="2026-03-31 00:00:00")],
                          "GINCOME": [income_row()]}):
            out = cn.fetch("600519", "2026-06-30")
        self.assertEqual(out["report_date"], "2026-06-30")   # 以利润表为准
        self.assertTrue(any("不一致" in n for n in out["notes"]))


class TestNoDataAtAll(unittest.TestCase):

    def test_all_templates_empty_is_not_an_exception(self):
        with _StubTables({}):
            out = cn.fetch("600519")
        self.assertIsNone(out["industry"])
        self.assertIsNone(out["report_date"])
        self.assertTrue(all(v is None for v in _all_values(out)))
        self.assertIn("不在东财 F10 覆盖内", " ".join(out["notes"]))
        self.assertIn("这不是网络失败", " ".join(out["notes"]))


# ---------------------------------------------------------------------------
# 网络失败 ≠ 没有数据
# ---------------------------------------------------------------------------

class TestNetworkFailures(unittest.TestCase):
    """东财掐连接时**不给任何响应** —— 不能把它当成「没有数据」。"""

    def test_retries_then_raises_with_all_three_possibilities(self):
        import http.client
        attempts = []

        def boom(*a, **k):
            attempts.append(1)
            raise http.client.RemoteDisconnected("Remote end closed connection")

        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = boom
            try:
                with self.assertRaises(cn.CnFinancialsError) as cm:
                    cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30")
            finally:
                net.guarded_get = orig

        self.assertGreaterEqual(len(attempts), 3, "重试次数太少，一次断连就放弃了")
        self.assertEqual(len(attempts), len(cn._RETRY_WAITS) + 1,
                         "重试次数与退避表不一致 —— 改了一处忘了另一处")
        waits = list(cn._RETRY_WAITS)
        self.assertEqual(waits, sorted(waits), "退避没有递增，限流时会越撞越糟")
        self.assertGreater(waits[-1], waits[0] * 2, "最后一次退避没有明显变长")

        msg = str(cm.exception)
        for token in ("代码", "网络", "限流", "直连"):
            self.assertIn(token, msg)
        self.assertIn("这不是「这家没有财务数据」", msg,
                      "必须写明：失败不等于没有数据")
        self.assertIn("可比集合", msg)

    def test_http_error_also_retries_and_raises(self):
        def boom(*a, **k):
            raise OSError("502 Bad Gateway")

        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = boom
            try:
                with self.assertRaises(cn.CnFinancialsError):
                    cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30")
            finally:
                net.guarded_get = orig

    def test_succeeds_after_a_transient_drop(self):
        state = {"n": 0}

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                import http.client
                raise http.client.RemoteDisconnected("boom")
            return payload([income_row()])

        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = flaky
            try:
                rows = cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30")
            finally:
                net.guarded_get = orig
        self.assertEqual(len(rows), 1)

    def test_broken_json_raises_instead_of_reporting_no_data(self):
        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = lambda *a, **k: b"<html>502</html>"
            try:
                with self.assertRaises(cn.CnFinancialsError) as cm:
                    cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30")
            finally:
                net.guarded_get = orig
        self.assertIn("无法解析", str(cm.exception))
        self.assertIn("没有数据", str(cm.exception))

    def test_empty_http_200_is_not_an_error(self):
        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = lambda *a, **k: empty_payload()
            try:
                self.assertEqual(cn._fetch_rows("600519.SH", "GINCOME", "2019-03-31"), [])
            finally:
                net.guarded_get = orig


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

class TestCaching(unittest.TestCase):

    def test_second_call_does_not_hit_the_network(self):
        calls = []

        def fake(url, query, timeout=30, headers=None):
            calls.append(url)
            return payload([income_row()])

        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = fake
            try:
                first = cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30")
                second = cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30")
            finally:
                net.guarded_get = orig
        self.assertEqual(len(calls), 1, "第二次应该命中缓存")
        self.assertEqual(first, second)

    def test_empty_responses_are_not_cached(self):
        """空响应可能来自限流下的异常返回 —— 缓存它是**粘的**，会固化成一个假结论。"""
        calls = []

        def fake(url, query, timeout=30, headers=None):
            calls.append(url)
            return empty_payload()

        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = fake
            try:
                cn._fetch_rows("600519.SH", "GINCOME", "2019-03-31")
                cn._fetch_rows("600519.SH", "GINCOME", "2019-03-31")
            finally:
                net.guarded_get = orig
        self.assertEqual(len(calls), 2, "空响应不该被缓存")

    def test_cache_can_be_bypassed(self):
        calls = []

        def fake(url, query, timeout=30, headers=None):
            calls.append(url)
            return payload([income_row()])

        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = fake
            try:
                cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30", use_cache=False)
                cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30", use_cache=False)
            finally:
                net.guarded_get = orig
        self.assertEqual(len(calls), 2)

    def test_different_periods_do_not_share_a_cache_entry(self):
        calls = []

        def fake(url, query, timeout=30, headers=None):
            calls.append(url)
            return payload([income_row()])

        with _IsolatedCache():
            orig = net.guarded_get
            net.guarded_get = fake
            try:
                cn._fetch_rows("600519.SH", "GINCOME", "2026-06-30")
                cn._fetch_rows("600519.SH", "GINCOME", "2025-12-31")
            finally:
                net.guarded_get = orig
        self.assertEqual(len(calls), 2)


# ---------------------------------------------------------------------------
# 出境闸门
# ---------------------------------------------------------------------------

class TestEgressGate(unittest.TestCase):
    """走项目自己的出口 —— 用 `extra_hosts` 显式声明主机，**不动 `net.py` 的白名单**。"""

    def test_host_is_declared_by_this_module(self):
        self.assertIn(cn.HOST, cn.EXTRA_HOSTS)
        self.assertIn(cn.HOST, net.allowed_hosts(cn.EXTRA_HOSTS))

    def test_queries_declare_the_host(self):
        q = cn._query({"a": "1"}, purpose="测试")
        self.assertIn(cn.HOST, q.extra_hosts)

    def test_filter_passes_the_gate_validation(self):
        """filter 会先过 `PublicQuery` 的长度/字符集校验 —— 不能绕过闸门。"""
        filt = cn._filter_for("600519.SH", "2026-06-30")
        self.assertLess(len(filt), net.MAX_VALUE_LEN)
        cn._query({"filter": filt}, purpose="测试")     # 不抛 = 合格

    def test_tests_do_not_edit_the_global_whitelist(self):
        """这条是给维护者的提示：本模块只**声明**自己要用的主机。

        若哪天有人把 datacenter 并进了 `net.py` 的全局白名单，这条断言会提醒他
        回来把本模块的写法与文档一起对齐（那时就可以不用 extra_hosts 了）。
        """
        self.assertTrue(cn.EXTRA_HOSTS.isdisjoint(net.DEFAULT_ALLOWED_HOSTS)
                        or cn.HOST in net.DEFAULT_ALLOWED_HOSTS)

    def test_url_is_built_from_validated_code(self):
        """代码先过 6 位数字校验才拼进 filter —— 换行/引号进不去。"""
        with self.assertRaises(cn.CnFinancialsError):
            cn.fetch('600519")&x="', "2026-06-30")


# ---------------------------------------------------------------------------
# 源自己声明的属性 + 规格（钉住文档里的模板名与字段名）
# ---------------------------------------------------------------------------

class TestSourceDeclaration(unittest.TestCase):

    def test_declares_market_and_currency(self):
        self.assertEqual(cn.MARKET, "cn")
        self.assertEqual(cn.CURRENCY, "CNY")

    def test_declares_no_prices(self):
        """财务源没有行情 —— 市值/EV 得配 `prices.py`。"""
        self.assertIs(cn.HAS_PRICES, False)

    def test_template_names_match_the_doc(self):
        self.assertEqual(cn.TEMPLATES["通用"], ("GINCOME", "GBALANCE"))
        self.assertEqual(cn.TEMPLATES["银行"], ("BINCOME", "BBALANCE"))
        self.assertEqual(cn.TEMPLATES["保险"], ("IINCOME", "IBALANCE"))
        self.assertEqual(cn.TEMPLATES["证券"], ("SINCOME", "SBALANCE"))
        self.assertEqual(cn.REPORT_PREFIX, "RPT_F10_FINANCE_")

    def test_endpoint_matches_the_doc(self):
        self.assertEqual(
            cn.BASE,
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        )

    def test_field_names_match_the_doc(self):
        """字段名逐字来自 `docs/数据源-中港财务.md` 的实测表。"""
        by_key = {c.key: c.candidates for c in cn.CONCEPTS}
        self.assertEqual(by_key["营业收入"],
                         ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"))
        self.assertEqual(by_key["净利润(归母)"], ("PARENT_NETPROFIT",))
        self.assertEqual(by_key["营业利润"], ("OPERATE_PROFIT",))
        self.assertEqual(by_key["所有者权益(归母)"], ("TOTAL_PARENT_EQUITY",))
        self.assertEqual(by_key["所有者权益合计"], ("TOTAL_EQUITY",))

    def test_equity_is_attributed_to_the_balance_sheet(self):
        """权益在资产负债表上，利润表那三个在利润表上 —— 走错表会静默取空。"""
        tables = {c.key: c.table for c in cn.CONCEPTS}
        self.assertEqual(tables["营业收入"], cn.INCOME)
        self.assertEqual(tables["营业利润"], cn.INCOME)
        self.assertEqual(tables["所有者权益(归母)"], cn.BALANCE)


@unittest.skipUnless(
    os.environ.get("FREEANALYST_NET_TESTS") == "1",
    "需要真实网络：设 FREEANALYST_NET_TESTS=1",
)
class TestAgainstRealEastmoney(unittest.TestCase):
    """对真实接口的回归。数字会变，所以只断言**结构与口径**，不断言数值。"""

    CASES = [
        ("600519", "通用"),
        ("000001", "银行"),
        ("601398", "银行"),
        ("600030", "证券"),
    ]

    def test_all_four_resolve_with_values(self):
        for code, industry in self.CASES:
            with self.subTest(code=code):
                out = cn.fetch(code, industry=industry)
                self.assertEqual(out["industry"], industry)
                self.assertIsNotNone(out["report_date"])
                for key in cn.REQUIRED:
                    self.assertIsNotNone(out["values"][key]["value"],
                                         f"{code} 的 {key} 没取到")
                    self.assertTrue(out["values"][key]["field"])

    def test_detection_matches_an_explicit_industry(self):
        """自动判断与显式指定必须一致 —— 探测逻辑错了会安静地用错模板。"""
        for code, industry in self.CASES:
            with self.subTest(code=code):
                self.assertEqual(cn.detect_industry(cn.secucode(code)), industry)


if __name__ == "__main__":
    unittest.main()
