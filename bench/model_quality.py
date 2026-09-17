#!/usr/bin/env python3
"""本地模型质量评测 —— 一个本地模型够不够格做尽调？

    python3 bench/model_quality.py                        # 测默认模型
    python3 bench/model_quality.py --models a,b,c         # 多模型对比
    python3 bench/model_quality.py --show-answers         # 打印模型原文

## 为什么要有这个

"能不能跑"和"能不能用"是两件事。小模型能让流程跑通，但会犯三类致命错误：

  1. 主体搞混 —— 把实控人的个人回购义务说成公司的财务压力
  2. 跨文档冲突检测不出来 —— CIM 说条款已定，访谈纪要里对方其实有保留
  3. 编造内容 —— 材料里没有的文件，它替你把结论写出来

这三类错误在尽调场景里会直接导致错误结论。

## 判分方式（v2）

**v1 用关键词匹配，产生了假通过**：模型在「材料缺口」节里写了"材料未提供"
（这是格式要求的），就从"结论"节里编造内容的行为中蒙混过关。

v2 改成：

  - **按节解析** —— 把答案拆成「结论 / 材料缺口 / 风险提示」三节，每节独立判分。
    某一节写得漂亮，救不了另一节说错话。
  - **来源覆盖** —— 跨文档冲突那条必须同时引用 CIM 和访谈纪要。只引用一边
    说明压根没做比对，哪怕结论碰巧对。
  - **语义模式** —— 用正则覆盖整个表述族（"对公司……压力/影响/负担"），
    而不是死盯一个固定说法。

**如果本评测全绿，说明这个模型可以进生产。** 不是"更强"，是"够格"。

## ⚠️ 引用成绩前先看有没有 `-rescored` 版本

`results-*.json` 里同一轮常有两份：

    results-v4-with-units.json            4/5   未达门槛   ← 原始判分
    results-v4-with-units-rescored.json   5/5   ★ 够格进生产  ← 判据修好之后重判

**引用没重判过的那份会得出反的结论。** 踩过：报告里把 `qwen3:14b` 说成
「4/5，卡在主体识别」，实际上它 5/5 —— 那 4/5 是判据的 bug
（把「**不直接构成**公司层面的财务压力」这句正确答案判成了「说错主体」）。

理由见 `rescore()`：**答案没变，变的只是尺子**，所以不该重跑模型
（既慢又会因采样随机性引入新变量）。

## 自检

换模型之前，先拿一个**已知不合格**的模型跑一遍，确认它会失败。
如果它全过，说明评测本身失效了 —— 这是比模型不合格更严重的问题。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from freeanalyst import (  # noqa: E402
    SYSTEM_PROMPT,
    build_context,
    call_local_model,
)
from retrieval import BM25, Chunk, chunk_document  # noqa: E402

SECTIONS = ["结论", "材料缺口", "风险提示"]


def corpus_chunks() -> list[Chunk]:
    """从 `corpus/` **现建**索引，不用 `index/chunks.json`。

    ## 为什么不能用环境里的索引（实测踩到）

    跑基准测试前刚把 Fitbit 10-K 入库，`load_index()` 读到的是那份 10-K，
    而用例问的是样例 CIM 里的事。

    **测试照样跑完，只是测的完全是另一份材料** —— 结果毫无意义，却不报错。

    评测必须自带材料，不能依赖环境状态。跑之前先 `ingest` 到哪儿、
    上次留了什么，都不该影响判分。
    """
    chunks: list[Chunk] = []
    n = 0
    corpus = ROOT / "corpus"
    for path in sorted(corpus.glob("*.txt")) + sorted(corpus.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for c in chunk_document(path.name, text):
            n += 1
            c.chunk_id = f"S{n}"
            chunks.append(c)
    return chunks

# ---------------------------------------------------------------------------
# 主体识别用的几个模式
#
# ## 设计说明（这一段值得读）
#
# 这里试过三种思路，前两种都在原地打转：
#
# 1. **靠正则认否定词** —— 加 `不构成/不产生`，漏了 `不直接承担`；
#    往否定表里加裸 `不`，又会放过「公司**不仅**面临回购压力」。
# 2. **靠正则认语序** —— 要求「公司 → 动词 → 负担词」。
#    但中文里「会**加重公司**的资金负担」是动词在前，三条判对用例直接挂了。
#
# 兜圈子是因为在解一个**开放集合**问题：「哪些说法算把义务说成公司的」说不完。
#
# 3. **改成要求「必须说对」（封闭集合）** ——
#    结论节必须明确否定公司在承担该义务。这可以判定，也不会随措辞漂移。
#    正则退居次要，只用来抓最直白的搞混，以及给出准确的失败原因。
#
# **教训：当一个检查规则已经调了三轮还在漏，说明问题不是规则不够细，
# 而是评判的角度选错了。**
# ---------------------------------------------------------------------------

# 次要检查：最直白的「把公司当义务人」表述。
# 不做精细否定判断 —— 精细判断交给 check_subject 里的要求式检查。
#
# 间隔里**排除逗号**：否则片段会跨过句子去咬下一个分句的词，
# 实测因此匹配到「公司将面临回购压力，实控人需承担无限连带责任」整段。
_COMPANY_BURDEN = re.compile(
    r"(公司|本公司|标的公司)[^。；，\n]{0,20}"
    r"(压力|影响|负担|冲击|紧张|重大|责任|义务)"
)
_NEGATION = re.compile(r"(无|没有|不存在|不|非|未|不再|无需|不属于|不由)")
# 看着像否定、其实不是的：
#   「不仅/不但」是递进
#   「无限」是 unlimited —— 实测把它当成否定，放过了「公司将面临回购压力」
_NOT_NEGATION = re.compile(r"(不仅|不但|不只|不光|无限)")

# 「某个人是义务人」的表述 —— 用来区分「彻底搞混主体」和「表述混乱但点了人」
_INDIVIDUAL_BEARER = re.compile(
    r"(实控人|实际控制人|董事长|股东|创始人)[^。；\n]{0,8}个人"
    r"|由个人承担|个人承担|个人的义务"
)

# 编造确认函结论的表述族
#
# 曾经有个**假阳性**：模式里有个宽松的分支 `确认函[^。\n]{0,10}(确认|显示|表明|指出)`，
# 它匹配到了「材料未提供客户方确认函的内容，**仅表明**董事长表示"回去找一下"」——
# 这是一句**完全正确**的回答（说的是材料里只有董事长那句话），却被判为编造。
#
# 改法：只匹配「声称确认函得出了结论」和「把未核实的事说成已了结」两种，
# 不再匹配中性的「表明/显示/指出」。
_FABRICATED = re.compile(
    r"(已闭环|已经闭环|不会再发生|不会再次发生|问题已解决|已得到澄清"
    r"|确认函[^。\n]{0,15}(已确认|确认了|已证实|证实了|已明确|载明|的结论是|结论为))"
)

# 承认信息缺失的表述族
_ADMIT_MISSING = re.compile(r"(未提供|没有提供|不存在|未包含|未提及|材料中未|材料里没有)")

# 「该义务不落在公司身上」的表述族。
# **这里必须宽** —— 漏掉一种说法就等于把正确答案判成错的。
_NON_COMPANY_OBLIGATION = re.compile(
    r"(不构成|不直接|不落在|不影响|非公司|个人义务|由其个人|属于个人|"
    r"不承担|不背负|无需承担|由个人承担|个人承担|公司不|非由公司|不属于公司|"
    r"而非公司|而不是公司|不在公司)"
)


#: 小句边界 —— 否定词只看同一小句内的，不跨逗号/分号/句号
_CLAUSE_BREAK = "。；，、！？\n"


def find_company_burden(text: str) -> str | None:
    """找出最直白的「把个人义务说成公司负担」表述。

    **这是次要检查。** 主检查是「结论节必须明确否定公司在承担该义务」
    （见 `check_subject`）—— 那个是封闭集合，可判定；这个是开放集合，
    只能抓最直白的形态，用来给出准确的失败原因。

    ## 否定词可能在片段**之前**（实测踩过一次）

    原实现只在匹配片段**内部**找否定词：

        片段（旧）  公司层面的财务压力            ← 没有否定词 → 判成"把义务说成公司负担"
        原句        不直接构成公司层面的财务压力  ← 否定词在这里，片段之外

    于是**完全正确的答案被判成错**。

    改成往前看，但**限定在同一小句内** —— 跨逗号会误伤：
    「若未完成IPO，公司将面临回购压力」里的「未」不在同一小句，
    不该算作对后半句的否定。
    """
    for m in _COMPANY_BURDEN.finditer(text):
        seg = m.group(0)
        start = max((text.rfind(ch, 0, m.start()) for ch in _CLAUSE_BREAK),
                    default=-1) + 1
        window = text[start:m.end()]
        if _NEGATION.search(_NOT_NEGATION.sub("只", window)):
            continue
        return seg
    return None


def describe_burden(span: str) -> str:
    """把匹配到的片段翻译成准确的批评。"""
    if _INDIVIDUAL_BEARER.search(span):
        return (f"把个人义务框在了公司头上：「{span}」——"
                f"句子里虽然点了义务人，但主语落在公司，读者会以为公司在承担")
    return f"把个人义务说成公司负担：「{span}」"


def split_sections(answer: str) -> dict[str, str]:
    """把答案按 ## 标题拆开。缺失的节返回空字符串。"""
    out: dict[str, str] = {}
    positions: list[tuple[int, str]] = []
    for sec in SECTIONS:
        for pattern in (f"## {sec}", f"##{sec}", f"**{sec}**", sec):
            idx = answer.find(pattern)
            if idx >= 0:
                positions.append((idx, sec))
                break
    positions.sort()
    for i, (idx, sec) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(answer)
        body = answer[idx:end]
        body = re.sub(r"^##?\s*\S+\s*", "", body, count=1)
        out[sec] = body.strip()
    for sec in SECTIONS:
        out.setdefault(sec, "")
    return out


# ---------------------------------------------------------------- 各用例判分

def check_subject(answer: str, hits: list[tuple[Chunk, float]]) -> list[str]:
    """主体识别：回购义务人是实控人个人，不是公司。

    ## 主检查是「必须说对」，不是「必须不写错」

    结论节必须**明确排除公司**。只写「实控人个人承担」还不够 ——
    要写清该义务不落在公司身上。

    为什么用要求式而不是禁止式，见文件上方那一段设计说明：
    「哪些说法算把义务说成公司的」是开放集合，禁止式检查怎么补都在漏；
    「必须明确排除公司」是封闭集合，可以判定。

    ## 为什么要看「风险提示」

    实测踩到的：模型在结论节写对了（「回购义务人为实控人个人」），
    却在风险提示节写「若未完成 IPO，**公司将面临回购压力**」。

    语料原文是「回购义务人为实控人个人，承担无限连带责任」，
    **没有任何"公司承担回购"的说法**。所以那句话就是主体搞混。
    """
    secs = split_sections(answer)
    concl = secs["结论"]

    if not concl:
        return ["缺少「结论」分节，无法判分"]

    reasons: list[str] = []

    # 主检查：结论节必须点名个人，并明确排除公司
    if not re.search(r"(实控人|董事长|实际控制人|股东)[^。\n]{0,10}个人", concl):
        reasons.append("结论节未指明义务主体是实控人个人")
    if not _NON_COMPANY_OBLIGATION.search(concl):
        reasons.append(
            "结论节没有明确排除公司 —— 只写「实控人个人」不够，"
            "必须说清该义务不落在公司身上"
        )

    # 次要检查：抓最直白的搞混，用来给出准确的失败原因
    burden = find_company_burden(f"{concl}\n{secs['风险提示']}")
    if burden:
        reasons.append(describe_burden(burden))

    return reasons


def check_conflict(answer: str, hits: list[tuple[Chunk, float]]) -> list[str]:
    """跨文档冲突：必须真的去比对了 CIM 和访谈纪要。"""
    reasons: list[str] = []
    src_of = {c.chunk_id: c.source.lower() for c, _ in hits}
    cited = set(re.findall(r"\[(S\d+)\]", answer))
    cited_srcs = {src_of[i] for i in cited if i in src_of}

    if not any("cim" in s for s in cited_srcs):
        reasons.append("未引用 CIM 片段")
    if not any("notes" in s for s in cited_srcs):
        reasons.append("未引用访谈纪要 —— 说明未做跨文档比对")

    if not re.search(r"(未达成一致|有顾虑|有保留|并未锁定|尚未锁定|冲突|不一致|未最终|仍待)", answer):
        reasons.append("未指出该条款在谈判中并未锁定")

    return reasons


def check_format(answer: str, hits: list[tuple[Chunk, float]]) -> list[str]:
    """格式遵循：三节齐全，且缺口节要有实质内容。"""
    reasons: list[str] = []
    secs = split_sections(answer)

    for sec in SECTIONS:
        if not secs[sec]:
            reasons.append(f"缺少分节「{sec}」")

    gap = secs["材料缺口"]
    if gap and len(gap) < 15:
        reasons.append(f"「材料缺口」过于敷衍（{len(gap)} 字）：{gap[:20]!r}")
    if gap and re.fullmatch(r"[材料]*未提供[。.]?", gap.strip()):
        reasons.append("「材料缺口」只写了『材料未提供』，未列出具体缺口")

    return reasons


def check_no_hallucination(answer: str, hits: list[tuple[Chunk, float]]) -> list[str]:
    """拒答幻觉：材料里没有的，不许替它写结论。"""
    reasons: list[str] = []
    concl = split_sections(answer)["结论"]

    if not concl:
        return ["缺少「结论」分节，无法判分"]

    fab = _FABRICATED.search(concl)
    if fab:
        reasons.append(f"结论节编造了材料中不存在的内容：「{fab.group(0)}」")

    if not _ADMIT_MISSING.search(concl):
        reasons.append("结论节未声明该信息材料中未提供")

    return reasons


#: 金额 + 单位 → 折算成「元」的乘数
_UNIT_MULT = {
    # 中文
    "亿元": 1e8, "亿": 1e8,
    "万元": 1e4, "万": 1e4,
    "千美元": 1e3, "万美元": 1e4, "百万美元": 1e6, "十亿美元": 1e9,
    "美元": 1.0, "人民币": 1.0, "元": 1.0,
    # 英文 —— **跨语言回答时模型几乎一定用这些词**
    # （实测：中文材料 + 英文提问 → 模型写 "18,300 million RMB"）
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "million": 1e6, "mn": 1e6, "m": 1e6,
    "thousand": 1e3, "k": 1e3,
    "usd": 1.0, "rmb": 1.0, "cny": 1.0,
}

_UNIT_ALT = "|".join(sorted((re.escape(u) for u in _UNIT_MULT), key=len, reverse=True))

_AMOUNT = re.compile(rf"([\d,]+(?:\.\d+)?)\s*({_UNIT_ALT})", re.I)

#: 表格的表头会声明整表单位，行里只有裸数字。
#:     中文： （单位：万元）  /  单位：千美元
#:     英文： $ in Thousands  /  (In thousands)  /  (USD in millions)
_UNIT_DECL_CN = re.compile(r"单位[:：]\s*(人民币)?\s*(亿元|万元|千美元|万美元|百万美元|美元|元)")
_UNIT_DECL_EN = re.compile(
    r"(?:in|In)\s+(thousands|millions|billions)", re.I
)
_EN_UNIT_MULT = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}


def declared_unit(text: str) -> float | None:
    """从文本里找「整表单位声明」，返回乘数。

    **不找这个的话，材料侧的金额一个都抽不到** ——
    财务报表的数字都在表里，单位写在表头（`$ in Thousands`），
    行里只有裸数字。实测时因为没有这一步，材料侧抽出来是空的，
    整个检查等于没做。
    """
    m = _UNIT_DECL_CN.search(text)
    if m:
        return _UNIT_MULT.get(m.group(2))
    m = _UNIT_DECL_EN.search(text)
    if m:
        return _EN_UNIT_MULT.get(m.group(1).lower())
    return None


def _amounts(text: str, apply_declared: bool = False) -> list[tuple[float, str]]:
    """抽出文本里所有金额 → [(折算成元的绝对值, 原文片段)]。

    `apply_declared=True` 时，文本里**没有单位的裸数字**也按整表单位折算。
    材料侧要用这个（表格行是裸数字）；答案侧不要用
    （答案里的裸数字多是年份、页码，不是金额）。
    """
    out: list[tuple[float, str]] = []
    for m in _AMOUNT.finditer(text):
        raw_num, unit = m.group(1), m.group(2)
        mult = _UNIT_MULT.get(unit.lower()) if unit.lower() in _UNIT_MULT else _UNIT_MULT.get(unit)
        if mult is None:
            continue
        try:
            v = float(raw_num.replace(",", ""))
        except ValueError:
            continue
        out.append((abs(v) * mult, m.group(0).strip()))

    if apply_declared:
        decl = declared_unit(text)
        if decl:
            for m in re.finditer(r"([\d,]+(?:\.\d+)?)(?!\s*(?:%|年|页))", text):
                raw = m.group(1)
                # 年份、页码这类不是金额
                try:
                    v = float(raw.replace(",", ""))
                except ValueError:
                    continue
                if 1900 <= v <= 2100 and "," not in raw:
                    continue
                out.append((abs(v) * decl, raw))
    return out


def check_units(answer: str, hits: list[tuple[Chunk, float]]) -> list[str]:
    """单位纪律：答案里的每个金额，都必须能对上材料里的金额。

    ## 两个真实失败（都是这个用例存在的理由）

    **Fitbit FY2016 10-K（千美元）**
        材料   Operating income (loss)  $ (112,465)      = 1.125 亿
        模型   营业利润 -112,465千美元（即亏损 11.25 亿美元）   ← 差 10 倍

    **宁波精塑 CIM（万元）**
        材料   2025 年营业收入 18,300 万元                 = 1.83 亿
        模型   Revenue 18,300 million RMB                  ← 差 100 倍

    两次都是**数量级错误**，而且原文数字就在旁边、看着是对的。

    ## 判法

    不禁止换算（跨语言回答时，把「万元」讲清楚是必要的），
    但**换算必须落在材料里的某个金额上**：
    答案里出现 1.83 亿 → 材料里有 18,300 万元 ✓
    答案里出现 216.95 亿 → 材料里没有任何金额等于它 ✗

    同数量的浮点误差（0.5%）内视为相等。
    """
    reasons: list[str] = []
    src_text = "\n".join(c.text for c, _ in hits)
    # 材料侧要应用「整表单位声明」—— 表格行里只有裸数字
    src = [a for a, _ in _amounts(src_text, apply_declared=True)]
    ans = _amounts(answer)

    if not ans:
        return reasons

    if not src:
        return reasons

    for value, raw in ans:
        if any(abs(value - s) <= max(s * 0.005, 1.0) for s in src):
            continue
        hint = declared_unit(src_text)
        unit_hint = f"（材料里的单位声明是 {hint:g} 倍）" if hint else "（注意「万」≠ million）"
        reasons.append(
            f"金额「{raw}」在材料里找不到对应 —— 换算改变了数量级{unit_hint}"
        )
    return reasons


CASES = [
    {
        "id": "units",
        "name": "单位纪律",
        "why": "自行换算会引入 10 倍量级的错误，而且前面那个数还是对的，最容易被放过",
        "question": "标的公司 2025 年的营业收入和报表 EBITDA 分别是多少？",
        "check": check_units,
        "note": "正确答法：原样引用「18,300 万元」「3,150 万元」，不要换算成亿",
    },
    {
        "id": "subject",
        "name": "主体识别",
        "why": "把实控人的个人义务说成公司的负担，是尽调里最危险的错误",
        "question": "回购义务对公司会造成什么财务压力？",
        "check": check_subject,
        "note": "正确答法：指出义务人是实控人个人，对公司不构成直接财务压力",
    },
    {
        "id": "conflict",
        "name": "跨文档冲突",
        "why": "尽调的全部价值就在这：协议写的和谈判中锁定的常常不是一回事",
        "question": "回购条款目前是否已经锁定？对我们是否有利？",
        "check": check_conflict,
        "note": "正确答法：同时引用 CIM 条款与访谈纪要，指出实控人有保留、未达成一致",
    },
    {
        "id": "format",
        "name": "格式遵循",
        "why": "输出要进投委会，结构不稳定就没法进流程",
        "question": "这个标的的主要风险是什么？",
        "check": check_format,
        "note": "正确答法：三节齐全，缺口节列出具体待补材料",
    },
    {
        "id": "no_hallucination",
        "name": "拒答幻觉",
        "why": "一个会编数字的尽调助手，比没有助手更危险",
        "question": "客户方确认函里对质保问题给出的结论是什么？",
        "check": check_no_hallucination,
        "note": "正确答法：指出材料中没有该确认函，不得替它编结论",
    },
]


# ---------------------------------------------------------------- 执行

def run_case(model: str, engine: BM25, case: dict) -> dict:
    hits = engine.search(case["question"], top_k=6)
    allowed = {c.chunk_id for c, _ in hits}
    user = f"【材料片段】\n{build_context(hits)}\n\n【问题】\n{case['question']}"

    try:
        answer = call_local_model(model, SYSTEM_PROMPT, user)
    except Exception as exc:  # noqa: BLE001
        return {"id": case["id"], "name": case["name"], "passed": False,
                "reasons": [f"调用失败：{type(exc).__name__}: {exc}"], "answer": ""}

    reasons = case["check"](answer, hits)

    dangling = set(re.findall(r"\[(S\d+)\]", answer)) - allowed
    if dangling:
        reasons.append(f"悬空引用 {sorted(dangling)}")

    return {"id": case["id"], "name": case["name"], "passed": not reasons,
            "reasons": reasons, "answer": answer}


def rescore(path: Path) -> int:
    """用当前的判分逻辑，重新判一遍**已保存的答案**。

    ## 为什么需要这个

    评测器本身会出错。实测中 `check_subject` 把
    「公司不承担，因此对公司无财务压力」判成了「把个人义务说成公司负担」——
    一个完全正确的答案被判错。

    发现判分逻辑有问题时，**不该重跑一遍模型**：那既慢（本地 30B 一次要 100 多秒），
    又会因为采样随机性引入新的变量。答案没变，变的只是尺子。

    检索是确定性的，所以可以重建 hits，然后原样重判。
    """
    saved = json.loads(path.read_text(encoding="utf-8"))
    chunks = corpus_chunks()
    engine = BM25(chunks)
    case_by_id = {c["id"]: c for c in CASES}

    print("=" * 74)
    print(f"重新判分：{path.name}")
    print("=" * 74)

    out: dict[str, dict] = {}
    for model, data in saved.items():
        results = []
        print(f"\n模型：{model}")
        for old in data["results"]:
            case = case_by_id.get(old["id"])
            if case is None:
                continue
            hits = engine.search(case["question"], top_k=6)
            reasons = case["check"](old["answer"], hits)
            allowed = {c.chunk_id for c, _ in hits}
            dangling = set(re.findall(r"\[(S\d+)\]", old["answer"])) - allowed
            if dangling:
                reasons.append(f"悬空引用 {sorted(dangling)}")
            new_pass = not reasons
            flag = ""
            if new_pass != old["passed"]:
                flag = "  ← 判分变化" + ("（原来误判为失败）" if new_pass else "（原来误判为通过）")
            print(f"  [{'通过' if new_pass else '失败'}] {case['name']}{flag}")
            for r in reasons:
                print(f"        ↳ {r}")
            results.append({"id": old["id"], "name": case["name"],
                            "passed": new_pass, "reasons": reasons,
                            "answer": old["answer"]})

        n = sum(1 for r in results if r["passed"])
        verdict = "★ 够格进生产" if n == len(results) else f"未达门槛（{n}/{len(results)}）"
        print(f"\n  小计：{n}/{len(results)} —— {verdict}   （原判分 {data['passed']}/{data['total']}）")
        out[model] = {"passed": n, "total": len(results),
                      "verdict": verdict, "results": results,
                      "previous_passed": data["passed"]}

    new_path = path.with_name(path.stem + "-rescored.json")
    new_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n重新判分结果已写入 {new_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地模型尽调质量评测")
    parser.add_argument("--models", default="qwen2.5-coder:7b", help="逗号分隔的模型名")
    parser.add_argument("--json", default="", help="把完整结果写到这个文件")
    parser.add_argument("--show-answers", action="store_true", help="打印模型原文")
    parser.add_argument("--rescore", default="", help="用当前判分逻辑重判已保存的结果文件")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每个模型跑几轮。temperature 0.1 仍有措辞抖动，"
                             "单轮结果不足以说明稳定；连着跑几轮看判定是否一致。")
    args = parser.parse_args()

    if args.rescore:
        return rescore(Path(args.rescore))

    chunks = corpus_chunks()
    if not chunks:
        print("索引为空。先运行：python3 freeanalyst.py ingest corpus", file=sys.stderr)
        return 1
    engine = BM25(chunks)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    report: dict[str, dict] = {}

    for model in models:
        print("=" * 74)
        print(f"模型：{model}" + (f"（跑 {args.repeat} 轮）" if args.repeat > 1 else ""))
        print("=" * 74)

        rounds: list[list[dict]] = []
        for _ in range(max(1, args.repeat)):
            round_results = []
            for case in CASES:
                r = run_case(model, engine, case)
                round_results.append(r)
                if args.repeat == 1:
                    print(f"  [{'通过' if r['passed'] else '失败'}] {case['name']}")
                    for reason in r["reasons"]:
                        print(f"        ↳ {reason}")
                    if args.show_answers or not r["passed"]:
                        body = r["answer"].strip().replace("\n", "\n        ")
                        print(f"        ┌ 模型原文\n        {body[:900]}\n")
            rounds.append(round_results)

        # 每一轮的得分
        scores = [sum(1 for r in rr if r["passed"]) for rr in rounds]
        n_pass, results = scores[-1], rounds[-1]

        # 逐用例的通过率 —— 判定稳不稳定看这个，不看单轮总分
        per_case_rate = []
        for i, case in enumerate(CASES):
            hits = sum(1 for rr in rounds if rr[i]["passed"])
            per_case_rate.append((case["name"], hits, len(rounds)))

        if args.repeat > 1:
            print(f"  每轮得分：{' / '.join(str(s) for s in scores)}"
                  f"   （满分 {len(CASES)}）")
            print()
            print("  逐用例通过率：")
            for name, hits, total in per_case_rate:
                # 不稳定 = 有的轮过、有的轮没过。
                # 0/N 是**稳定失败**，N/N 是**稳定通过**，两种都算稳定。
                flag = "" if hits in (0, total) else "   ← 不稳定"
                print(f"    {name:12} {hits}/{total}{flag}")
            if len(set(scores)) > 1:
                print()
                print("  ⚠ **各轮得分不一致。** 结论要用最差那轮，"
                      "不能用最好那轮 —— 生产里没有『重跑一次』这个选项。")

        verdict = ("★ 够格进生产"
                   if min(scores) == len(CASES)
                   else f"未达门槛（最差 {min(scores)}/{len(CASES)}）")
        print(f"\n  小计：{n_pass}/{len(CASES)} —— {verdict}\n")
        report[model] = {"passed": n_pass, "total": len(CASES),
                         "verdict": verdict, "results": results,
                         "rounds": scores,
                         "per_case": {n: f"{h}/{t}" for n, h, t in per_case_rate}}

    print("=" * 74)
    print("汇总")
    print("=" * 74)
    for model, d in report.items():
        bar = "".join("●" if r["passed"] else "○" for r in d["results"])
        print(f"  {model:<28} {bar}  {d['passed']}/{d['total']}  {d['verdict']}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n完整结果已写入 {args.json}")

    return 0 if all(d["passed"] == d["total"] for d in report.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
