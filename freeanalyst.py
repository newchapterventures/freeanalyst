#!/usr/bin/env python3
"""airlock —— 本地优先的投资尽调 agent。

    ingest   把材料切分、本地建索引（数据不出机器）
    ask      检索 + 本地模型作答，每条结论强制带原文出处
    audit    查看出网审计日志
    doctor   检查本地模型是否就绪

零第三方依赖。所有网络调用经过 guard.guarded_request()。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from guard import EgressBlocked, audit_summary, guarded_request
from retrieval import Chunk, BM25, chunk_document, load_text

ROOT = Path(__file__).resolve().parent
INDEX_DIR = ROOT / "index"
DEFAULT_MODEL = "qwen2.5-coder:7b"
OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"

SYSTEM_PROMPT = """你是一名投资尽调助手。你只能依据用户提供的【材料片段】回答。

铁律：
1. 每条结论后面必须标注来源编号，格式 [S1] 或 [S2][S3]。没有来源的话不要写。
2. 材料里没有的信息，一律写"材料未提供"，绝对禁止推测或用常识补全。
3. 数字必须原样引用，不要换算、不要四舍五入、不要改变符号或单位。
4. 如果材料之间存在冲突，明确指出冲突，不要替它下结论。
5. 区分义务主体。实控人、董事长、股东个人的义务不等于公司的义务。
   写"公司面临…压力/责任/义务"之前，先确认材料说的是公司还是个人。

输出格式：
## 结论
（逐条，每条带 [Sn] 出处）

## 材料缺口
（要回答这个问题但材料里没有的东西）

## 风险提示
（材料中明确写出的不利条款或异常数据；没有就写"未发现"）
"""


# ---------------------------------------------------------------- 索引

def index_path() -> Path:
    return INDEX_DIR / "chunks.json"


def save_index(chunks: list[Chunk]) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "chunk_id": c.chunk_id,
            "source": c.source,
            "para_start": c.para_start,
            "para_end": c.para_end,
            "text": c.text,
        }
        for c in chunks
    ]
    index_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_index() -> list[Chunk]:
    path = index_path()
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    chunks = [
        Chunk(
            chunk_id=item["chunk_id"],
            source=item["source"],
            para_start=item["para_start"],
            para_end=item["para_end"],
            text=item["text"],
        )
        for item in raw
    ]
    from retrieval import tokenize

    for c in chunks:
        c.tokens = tokenize(c.text)
    return chunks


def _load_material(path: Path) -> tuple[str, list[str]]:
    """读一份材料 → (文本, 提示信息)。

    `.txt` / `.md` 走标准库；`.htm` / `.html` 走标准库；`.pdf` 走 `ingest/pdf.py`
    （需要可选的 pdfplumber）。

    **三种格式都要过归一化。** 只归一化 PDF 是不够的 ——
    索引侧和查询侧口径不一致就会静默失配，那比不归一化还隐蔽。
    """
    from ingest.normalize import normalize_text

    ext = path.suffix.lower()
    notes: list[str] = []

    if ext == ".pdf":
        from ingest import pdf as ip
        doc = ip.extract_pdf(path)
        notes.append(doc.summary())
        return doc.to_text(), notes

    if ext in (".htm", ".html"):
        from ingest import html as ih
        doc = ih.extract_html(path)
        notes.append(f"{path.name}：{len(doc.tables)} 个表格")
        return doc.to_text(), notes

    return normalize_text(load_text(path)), notes


def cmd_ingest(args: argparse.Namespace) -> int:
    corpus = Path(args.path).expanduser().resolve()
    if not corpus.exists():
        print(f"找不到路径：{corpus}", file=sys.stderr)
        return 1

    patterns = ("*.txt", "*.md", "*.pdf", "*.htm", "*.html")
    files: list[Path] = []
    for pattern in patterns:
        files.extend(sorted(corpus.rglob(pattern)))
    files = [f for f in files if not f.name.startswith(".")]
    if not files:
        print(f"{corpus} 下没有 .txt / .md / .pdf / .htm / .html 材料", file=sys.stderr)
        return 1

    # 重新编号，保证 S编号全局唯一且可追溯
    all_chunks: list[Chunk] = []
    counter = 0
    failed: list[tuple[Path, str]] = []

    for path in files:
        rel = str(path.relative_to(corpus))
        try:
            text, notes = _load_material(path)
        except Exception as exc:  # noqa: BLE001
            # **取不到的材料必须报出来** —— 静默跳过会让索引少几份，
            # 而后面所有问答都会以"材料里没写"来解释这个缺失。
            failed.append((path, f"{type(exc).__name__}: {exc}"))
            continue
        for n in notes:
            print(f"  {n}")
        n_before = len(all_chunks)
        for chunk in chunk_document(rel, text):
            counter += 1
            chunk.chunk_id = f"S{counter}"
            all_chunks.append(chunk)
        if len(all_chunks) == n_before:
            failed.append((path, "抽出来的文本为空 —— 可能是扫描件（本工具不做 OCR）"))

    save_index(all_chunks)
    n_pdf = sum(1 for f in files if f.suffix.lower() == ".pdf")
    print(f"\n已入库 {len(files) - len(failed)}/{len(files)} 份材料 "
          f"（其中 PDF {n_pdf} 份）→ {len(all_chunks)} 个片段")
    print(f"索引位置：{index_path()}")

    if failed:
        print(f"\n⚠ {len(failed)} 份没能入库：")
        for p, why in failed:
            print(f"  ✗ {p.relative_to(corpus)}：{why}")
        print("  **不静默跳过** —— 少了材料会让后面的问答以「材料里没写」"
              "来解释这个缺失，而实际上是没读进来。")

    print("（全部处理在本机完成，未发生任何出网调用）")
    return 0 if not failed else 2


# ---------------------------------------------------------------- 问答

def call_local_model(model: str, system: str, user: str) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "stream": False,
        }
    ).encode("utf-8")

    raw = guarded_request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        purpose=f"local LLM inference ({model})",
    )
    body = json.loads(raw.decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def build_context(hits: list[tuple[Chunk, float]]) -> str:
    blocks = []
    for chunk, _score in hits:
        blocks.append(f"[{chunk.chunk_id}] 来源：{chunk.cite_label()}\n{chunk.text}")
    return "\n\n---\n\n".join(blocks)


def verify_citations(answer: str, allowed_ids: set[str]) -> dict:
    """校验模型引用的编号是否都真实存在——防悬空引用。"""
    cited = set(re.findall(r"\[(S\d+)\]", answer))
    return {
        "cited": sorted(cited),
        "dangling": sorted(cited - allowed_ids),
        "grounded": not (cited - allowed_ids),
    }


def cmd_ask(args: argparse.Namespace) -> int:
    chunks = load_index()
    if not chunks:
        print("索引为空。先运行：python3 airlock.py ingest <材料目录>", file=sys.stderr)
        return 1

    engine = BM25(chunks)
    hits = engine.search(args.question, top_k=args.top_k)
    if not hits:
        print("检索未命中任何片段。换个说法，或确认材料已入库。", file=sys.stderr)
        return 1

    allowed_ids = {c.chunk_id for c, _ in hits}
    user_prompt = f"【材料片段】\n{build_context(hits)}\n\n【问题】\n{args.question}"

    try:
        answer = call_local_model(args.model, SYSTEM_PROMPT, user_prompt)
    except EgressBlocked as exc:
        print(f"出网被拦截：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"连不上本地模型服务（{OLLAMA_URL}）。先运行 `ollama serve`。\n{exc}", file=sys.stderr)
        return 1

    check = verify_citations(answer, allowed_ids)

    print("=" * 68)
    print(f"问题：{args.question}")
    print(f"模型：{args.model}（本地） · 检索命中 {len(hits)} 个片段")
    print("=" * 68)
    print(answer.strip())
    print("-" * 68)
    print("检索到的片段（可追溯）：")
    for chunk, score in hits:
        preview = chunk.text.replace("\n", " ")[:58]
        print(f"  [{chunk.chunk_id}] {chunk.cite_label():<28} 相关度 {score:5.2f}  {preview}…")
    print("-" * 68)
    status = "通过" if check["grounded"] else f"发现悬空引用 {check['dangling']}"
    print(f"引用校验：{status}（引用 {check['cited'] or '无'}）")
    egress = audit_summary()
    external = egress["external_hosts"] or "无"
    print(f"本次出网目标：仅 127.0.0.1 · 累计外部主机：{external}")
    return 0


# ---------------------------------------------------------------- 运维

def cmd_audit(args: argparse.Namespace) -> int:
    summary = audit_summary()
    print(f"审计日志：{summary['path']}")
    print(f"  总调用   {summary['total']}")
    print(f"  放行     {summary['allowed']}  （应全部为回环地址）")
    print(f"  拦截     {summary['blocked']}")
    print(f"  外部主机 {summary['external_hosts'] or '无'}")
    print()
    print("注意：`拦截 0 / 外部主机 无` 才代表从未向外部发起过请求。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"Python      {sys.version.split()[0]}")
    print(f"项目目录    {ROOT}")
    print(f"索引        {'存在' if index_path().exists() else '未建立'}"
          f"（片段数 {len(load_index())}）")
    try:
        raw = guarded_request("http://127.0.0.1:11434/api/tags", purpose="health check")
        models = [m["name"] for m in json.loads(raw.decode()).get("models", [])]
        print(f"本地模型    {len(models)} 个：{', '.join(models) or '无'}")
    except Exception as exc:  # noqa: BLE001
        print(f"本地模型    未就绪（{exc}）")
    print(f"审计条目    {audit_summary()['total']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="airlock", description="本地优先的投资尽调 agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest", help="把材料入库到本机索引")
    p_ingest.add_argument("path", help="材料目录")
    p_ingest.set_defaults(func=cmd_ingest)

    p_ask = sub.add_parser("ask", help="基于本机索引提问")
    p_ask.add_argument("question")
    p_ask.add_argument("--model", default=DEFAULT_MODEL)
    p_ask.add_argument("--top-k", type=int, default=6)
    p_ask.set_defaults(func=cmd_ask)

    sub.add_parser("audit", help="查看出网审计").set_defaults(func=cmd_audit)
    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
