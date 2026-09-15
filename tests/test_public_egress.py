"""跨域防护测试 —— 证明机密数据出不去。

这个文件的核心用例不是"函数返回了正确值"，而是**用真实的语料文件试探，
确认它们出不去**。

## 重要背景

`net.py` 里的格式检查（长度、字符集、换行）**不足以防护**——实测中
一段 45 字的完整语料段落能顺利通过。所以主防线在 `privacy.py` 的
语料重叠检查。这里有一条测试专门把这个局限固定下来，
防止后来的人误以为格式检查够了。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="freeanalyst-public-test-")
os.environ["FREEANALYST_PUBLIC_AUDIT"] = str(Path(_TMP) / "egress-public.jsonl")

import net  # noqa: E402
import privacy  # noqa: E402

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"
CORPUS_TEXT = "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(CORPUS_DIR.glob("*.txt"))
)


class TestNetFormatChecks(unittest.TestCase):
    """格式检查层 —— 防误伤，不是主防线。"""

    def test_blocks_host_outside_whitelist(self):
        with self.assertRaises(net.PublicEgressBlocked):
            net.guarded_get("https://evil.example.com/x",
                            net.PublicQuery({"q": "AAPL"}, purpose="test"))

    def test_allows_whitelisted_sec_host(self):
        host = "data.sec.gov"
        self.assertIn(host, net.allowed_hosts())

    def test_rejects_overlong_value(self):
        with self.assertRaises(net.PublicEgressBlocked) as ctx:
            net.PublicQuery({"q": "x" * 200}, purpose="test")
        self.assertIn("长度", str(ctx.exception))

    def test_rejects_whitespace(self):
        """换行是材料文本的特征，查询参数不会有。"""
        with self.assertRaises(net.PublicEgressBlocked):
            net.PublicQuery({"q": "line1\nline2"}, purpose="test")

    def test_rejects_local_chunk_reference(self):
        """机密域的内部编号不该出现在出境请求里。"""
        for bad in ("[S3]", "sample-cim.txt#p7"):
            with self.subTest(bad=bad):
                with self.assertRaises(net.PublicEgressBlocked):
                    net.PublicQuery({"q": bad}, purpose="test")

    def test_rejects_nested_structure(self):
        with self.assertRaises(net.PublicEgressBlocked):
            net.PublicQuery({"q": {"nested": "dict"}}, purpose="test")

    def test_accepts_structured_params(self):
        q = net.PublicQuery({"concept": "Assets", "period": "CY2025Q1I", "limit": 5},
                            purpose="test")
        self.assertEqual(q.params["period"], "CY2025Q1I")


class TestFormatChecksAreNotEnough(unittest.TestCase):
    """把这个局限固定下来 —— 免得后来的人以为格式检查够了。

    现实：语料句子混着数字和标点，中文串被打断，"连续中文超过 N 个字就拒绝"
    这类规则触发不了；而放宽长度上限会连正常查询一起拦掉。
    """

    def test_corpus_sentence_passes_format_layer(self):
        sentence = "标的公司成立于 2011 年，位于浙江省宁波市北仑区，主营汽车电子与家电领域的精密注塑件。"
        # 格式层放行 —— 这不是 bug，是这一层的能力边界
        net.PublicQuery({"q": sentence}, purpose="format-layer-limit")
        # 真正的防线在下一层
        with self.assertRaises(privacy.PrivacyViolation):
            privacy.check_public_params({"q": sentence}, CORPUS_TEXT)


class TestCorpusOverlap(unittest.TestCase):
    """主防线：出境的值不能与本地语料有任何实质重合。"""

    def test_full_paragraph_blocked(self):
        s = "标的公司成立于 2011 年，位于浙江省宁波市北仑区，主营汽车电子与家电领域的精密注塑件。"
        with self.assertRaises(privacy.PrivacyViolation) as ctx:
            privacy.check_public_params({"q": s}, CORPUS_TEXT)
        self.assertIn("与本地语料重合", str(ctx.exception))

    def test_financial_sentence_blocked(self):
        s = "2025 年营业收入 18,300 万元，2023 年为 13,150 万元，两年复合增长率 17.9%。"
        with self.assertRaises(privacy.PrivacyViolation):
            privacy.check_public_params({"q": s}, CORPUS_TEXT)

    def test_short_sentence_blocked(self):
        s = "第一大客户为某合资 Tier1，占收入 34%。"
        with self.assertRaises(privacy.PrivacyViolation):
            privacy.check_public_params({"q": s}, CORPUS_TEXT)

    def test_punctuation_rewrite_does_not_evade(self):
        """改写标点、去掉空格不能绕过 —— 归一化会抹平这些差异。"""
        s = "2025年营业收入18300万元，2023年为13150万元。"
        with self.assertRaises(privacy.PrivacyViolation):
            privacy.check_public_params({"q": s}, CORPUS_TEXT)

    def test_overlap_inside_list_param(self):
        """藏在列表参数里也要拦住。"""
        s = "2025 年营业收入 18,300 万元，2023 年为 13,150 万元。"
        with self.assertRaises(privacy.PrivacyViolation):
            privacy.check_public_params({"terms": ["正常词", s]}, CORPUS_TEXT)

    def test_legitimate_query_passes(self):
        """股票代码、期间代码这类结构化查询必须能出去。"""
        privacy.check_public_params(
            {"ticker": "600096", "period": "CY2025Q1I", "concept": "Assets"},
            CORPUS_TEXT,
        )

    def test_industry_code_passes(self):
        """行业代码是合规的替代查询方式 —— 不点名标的，只用公开的行业口径。"""
        privacy.check_public_params({"industry": "C3670", "metric": "ev_ebitda"}, CORPUS_TEXT)

    def test_no_corpus_means_only_blocklist_applies(self):
        """没有语料时检查会退化 —— 这是配置问题，不是设计意图。"""
        privacy.check_public_params({"ticker": "600096"}, corpus_text="")

    def test_normalize_is_aggressive(self):
        """归一化要抹掉空白、标点、全角符号、大小写。"""
        a = privacy.normalize("2025 年，营业收入 18,300 万元。")
        b = privacy.normalize("2025年营业收入18300万元")
        self.assertEqual(a, b)


class TestBlocklist(unittest.TestCase):
    """标的公司名本身也是机密 —— 不能从查询日志里看出你在看哪家公司。"""

    def test_target_name_blocked(self):
        with self.assertRaises(privacy.PrivacyViolation) as ctx:
            privacy.check_public_params({"q": "宁波精塑 可比公司"},
                                        CORPUS_TEXT, blocklist={"宁波精塑"})
        self.assertIn("禁名", str(ctx.exception))

    def test_blocklist_file_parsing(self):
        p = Path(_TMP) / "blocklist.txt"
        p.write_text("# 注释行\n宁波精塑\n\n 精塑 \n", encoding="utf-8")
        terms = privacy.load_blocklist(p)
        self.assertEqual(terms, {"宁波精塑", "精塑"})

    def test_missing_blocklist_file_is_empty(self):
        self.assertEqual(privacy.load_blocklist(Path(_TMP) / "nope.txt"), set())


class TestSafePublicGet(unittest.TestCase):
    """机密域发起公开域请求的唯一入口 —— 两道闸的顺序不能反。"""

    def test_privacy_check_runs_before_network(self):
        """语料内容必须在触网之前就被拦下。"""
        s = "2025 年营业收入 18,300 万元，2023 年为 13,150 万元。"
        with self.assertRaises(privacy.PrivacyViolation):
            privacy.safe_public_get(
                "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
                {"q": s}, corpus_text=CORPUS_TEXT, purpose="test",
            )

    def test_blocked_host_never_reaches_network(self):
        with self.assertRaises(net.PublicEgressBlocked):
            privacy.safe_public_get("https://evil.example.com/x",
                                    {"q": "AAPL"}, corpus_text=CORPUS_TEXT, purpose="test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
