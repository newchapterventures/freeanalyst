"""量「解析成功率」—— 四个层面，只报计数不做判断。

    ① 页      总页 / 原生文字层 / 走 OCR / OCR 占比
    ② 表      三张表各自「映射行数 / 总行数」  ← 项目自己的 mapping_rates()
    ③ 关键科目 引擎离不了的科目里，哪些没映射上  ← missing_key_fields()
    ④ 金额    ok / repaired / suspect（只统计**真正走 OCR 的页**）

## 为什么它在仓库里、不在 /tmp

第一版写在 `/tmp`，跑完就没了 —— **那份量测因此不可复现**。
量测脚本属于项目资产：判据会变，成绩单必须能重跑。

## 两条踩过的坑

**① 不许对全文跑 OCR。** 第一版对 187 页原生文字层 PDF 全文跑了 OCR，
量出 14440 个格子 —— 但那份材料 `ocr_pages = 0`，流水线**根本不会去读**
那些 OCR 结果。**量的是不存在的东西。**

**② `ocr_pages` 的默认值 `last=0` 不是「到最后」**，而是 `max(缓存里的页)`
（冷缓存时只跑第 1 页）。所以这里**逐页显式传**。

## 用法

    python3 bench/measure_parse.py <目录或若干 PDF> [--out 结果.json]

带目录时只取 `.pdf`。结果里自带版本戳（`bench/stamp.py`），
所以迟到的成绩单能对上「这是哪一版」。

**打印的是文件名哈希，不是文件名** —— 材料名本身是机密。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench import stamp                      # noqa: E402
from ingest import ocr                       # noqa: E402
from ingest import pdf as ip                 # noqa: E402

#: 判据就在这些文件里 —— **必须一起哈希**。
#: 只哈希本脚本的话，「什么算映射上」改了、指纹不变，这道闸等于没有。
DEPS = [
    ROOT / "ingest" / "ocr.py",
    ROOT / "ingest" / "pdf.py",
    ROOT / "financials" / "from_pdf.py",
    ROOT / "financials" / "notes.py",
    ROOT / "financials" / "assemble.py",
    ROOT / "financials" / "statements.py",
    ROOT / "financials" / "canonical.py",
]


def tag(p: Path) -> str:
    """文件名哈希 —— 材料名是机密，只留可对照的短码。"""
    return hashlib.sha1(p.name.encode("utf-8")).hexdigest()[:8]


def amount_stats(p: Path, ocr_pages: list[int]) -> dict:
    """金额格质量 —— **只量真正走 OCR 的那几页**。

    ② `ocr_pages` 的默认值 `last=0` 不是「到最后」，所以这里逐页显式传。
    """
    from collections import Counter

    if not ocr_pages:
        return {"note": "全文有文字层，无需 OCR"}

    cnt: Counter = Counter()
    cells = 0
    shapes: Counter = Counter()
    try:
        for n in sorted(set(ocr_pages)):
            got = ocr.ocr_pages(p, n, n)
            for row in (got.get(n) or []):
                for cell in row:
                    text = str(cell[-1] if isinstance(cell, (list, tuple)) else cell)
                    cells += 1
                    if not ocr.is_amount_ish(text):
                        continue
                    _, status = ocr.parse_amount(text)
                    cnt[status] += 1
                    if status == "suspect":
                        # 数字换成 # —— 只看结构，不泄露数值
                        import re

                        shapes[re.sub(r"\d", "#", text)[:18]] += 1
    except Exception as exc:                     # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:110]}

    out: dict = dict(cnt)          # 显式 dict —— 下面还要塞 list/字典，不放宽会被类型检查挡
    out["cells"] = cells
    out["pages"] = len(set(ocr_pages))
    if shapes:
        out["suspect_shapes"] = dict(shapes.most_common(6))
    return out


def measure(p: Path) -> dict:
    out: dict = {"file": tag(p), "size_mb": round(p.stat().st_size / 1e6, 1)}
    t0 = time.time()

    try:
        doc = ip.extract_pdf(p)
        pages = list(getattr(doc, "pages", []) or [])
        opages = list(getattr(doc, "ocr_pages", []) or [])
        out["pages"] = len(pages)
        out["ocr_pages"] = len(opages)
        out["ocr_pct"] = round(100 * len(opages) / len(pages), 1) if pages else None
    except Exception as exc:                     # noqa: BLE001
        out["page_error"] = f"{type(exc).__name__}: {exc}"[:150]
        return out

    out["amounts"] = amount_stats(p, opages)

    try:
        from financials.from_pdf import load_pdf_statements

        st = load_pdf_statements(str(p))
        out["mapping"] = {n: [m, t] for n, m, t in st.mapping_rates()}
        out["missing_key"] = st.missing_key_fields()
        warns = st.mapping_warnings() if hasattr(st, "mapping_warnings") else []
        out["warnings"] = len(warns)
        out["unit_known"] = bool(getattr(st, "unit", ""))
        out["scope"] = getattr(st, "scope", "")[:14]
        out["period"] = getattr(st, "period", "")[:24]
    except Exception as exc:                     # noqa: BLE001
        out["statement_error"] = f"{type(exc).__name__}: {exc}"[:150]

    out["seconds"] = round(time.time() - t0, 1)
    return out


def collect(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a).expanduser()
        if p.is_dir():
            out += sorted(p.glob("*.pdf"), key=lambda x: x.stat().st_size)
        elif p.exists():
            out.append(p)
        else:
            print(f"  跳过（不存在）：{a}")
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    out_path: Path | None = None
    if "--out" in argv:
        i = argv.index("--out")
        out_path = Path(argv[i + 1]).expanduser()
        argv = argv[:i] + argv[i + 2:]

    files = collect(argv)
    if not files:
        print("没找到可量的 PDF")
        return 2

    print(f"  量测 {len(files)} 份 PDF（体积从小到大）\n", flush=True)
    results = []
    for p in files:
        print(f"[{tag(p)}] {p.stat().st_size / 1e6:.1f} MB …", flush=True)
        try:
            r = measure(p)
        except Exception as exc:                 # noqa: BLE001
            r = {"file": tag(p), "fatal": f"{type(exc).__name__}: {exc}"[:200]}
        results.append(r)
        print("   → " + json.dumps(r, ensure_ascii=False)[:520], flush=True)

    payload = stamp.attach({}, __file__, DEPS,
                           sample=f"{len(files)} 份 PDF",
                           files=len(files),
                           total_pages=sum(r.get("pages", 0) for r in results),
                           ocr_pages=sum(r.get("ocr_pages", 0) for r in results))
    payload["rows"] = results

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\n  已存 {out_path}")
    print("\n  " + stamp.show(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
