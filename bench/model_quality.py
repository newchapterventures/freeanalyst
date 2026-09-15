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
    load_index,
)
from retrieval import BM25, Chunk  # noqa: E402

SECTIONS = ["结论", "材料缺口", "风险提示"]

# 个人义务被误挂到公司头上的表述族
_COMPANY_BURDEN = re.compile(
    r"公司[^。；\n]{0,14}(现金流|财务|资金|偿债)[^。；\n]{0,10}(压力|影响|负担|冲击|重大|紧张)"
)
# 编造确认函结论的表述族
_FABRICATED = re.compile(
    r"(已闭环|已经闭环|不会再发生|不会再次发生|确认函[^。\n]{0,10}(确认|显示|表明|指出))"
)
# 承认信息缺失的表述族
_ADMIT_MISSING = re.compile(r"(未提供|没有提供|不存在|未包含|未提及|材料中未|材料里没有)")


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
    """主体识别：回购义务人是实控人个人，不是公司。"""
    reasons: list[str] = []
    concl = split_sections(answer)["结论"]

    if not concl:
        return ["缺少「结论」分节，无法判分"]

    burden = _COMPANY_BURDEN.search(concl)
    if burden:
        reasons.append(f"把个人义务说成公司负担：「{burden.group(0)}」")

    if not re.search(r"(实控人|董事长|实际控制人)[^。\n]{0,10}个人", concl):
        reasons.append("结论节未指明义务主体是实控人个人")

    if not re.search(r"(不构成|不直接|不落在|不影响|非公司|个人义务|由其个人|属于个人)", concl):
        reasons.append("结论节未说明该义务不落在公司身上")

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


CASES = [
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


def main() -> int:
    parser = argparse.ArgumentParser(description="本地模型尽调质量评测")
    parser.add_argument("--models", default="qwen2.5-coder:7b", help="逗号分隔的模型名")
    parser.add_argument("--json", default="", help="把完整结果写到这个文件")
    parser.add_argument("--show-answers", action="store_true", help="打印模型原文")
    args = parser.parse_args()

    chunks = load_index()
    if not chunks:
        print("索引为空。先运行：python3 freeanalyst.py ingest corpus", file=sys.stderr)
        return 1
    engine = BM25(chunks)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    report: dict[str, dict] = {}

    for model in models:
        print("=" * 74)
        print(f"模型：{model}")
        print("=" * 74)
        results = []
        for case in CASES:
            r = run_case(model, engine, case)
            results.append(r)
            print(f"  [{'通过' if r['passed'] else '失败'}] {case['name']}")
            for reason in r["reasons"]:
                print(f"        ↳ {reason}")
            if args.show_answers or not r["passed"]:
                body = r["answer"].strip().replace("\n", "\n        ")
                print(f"        ┌ 模型原文\n        {body[:900]}\n")

        n_pass = sum(1 for r in results if r["passed"])
        verdict = "★ 够格进生产" if n_pass == len(CASES) else f"未达门槛（{n_pass}/{len(CASES)}）"
        print(f"\n  小计：{n_pass}/{len(CASES)} —— {verdict}\n")
        report[model] = {"passed": n_pass, "total": len(CASES),
                         "verdict": verdict, "results": results}

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
