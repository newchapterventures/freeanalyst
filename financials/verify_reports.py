"""三份真实年报的输入层验证报告。

跑法：`python3 -m financials.verify_reports`
"""

from __future__ import annotations

import logging
import sys

logging.disable(logging.WARNING)

from pathlib import Path

from ingest import pdf as ip
from ingest.html import _to_number
from financials import canonical as cn
from financials import statements as stm

CASES = [
    ("贵州茅台 2025 年度报告（A 股 · 简体中文 · 原生文字层）",
     "/Users/fuweijia/Docs/AI/1b9fae59825c41bf9a776892a00565f7.pdf",
     {"balance": ("合并资产负债表", 3), "income": ("合并利润表", 3),
      "cash_flow": ("合并现金流量表", 3), "unit": "元", "gaap": "CAS"},
     "2025-12-31"),
    ("宝宝树集团 2020 年报（H 股 · 繁体中文 + 英文 · 原生文字层）",
     "/Users/fuweijia/Docs/Fosun Thinkpad/宝宝树/宝宝树年报2020.pdf",
     {"balance": ("Consolidated Statement of Financial Position", 1),
      "income": ("Consolidated Statement of Profit or Loss", 1),
      "cash_flow": ("Consolidated Statement of Cash Flows", 1),
      "unit": "千元", "gaap": "IFRS"},
     "2020-12-31"),
    ("苏州井利电子 2024 年审计报告（非上市 · **扫描件，走 OCR**）",
     "/Users/fuweijia/Docs/Search Fund/项目/苏州井利电子2024年审计报告.pdf",
     {"balance": ("资产负债表", 1), "income": ("利润表", 1),
      "cash_flow": ("现金流量表", 1), "unit": "元", "gaap": "CAS"},
     "2024-12-31"),
]


def build(doc, title, span):
    """按标题定位一张表。

    ## 为什么要挑「数据最多的那处」（实测踩到）

    标题在年报里会出现多次：目录页、合并报表、母公司报表。
    只取**第一次**出现的位置，三张表会全部落在目录页上 ——
    表现是三张表行数一模一样、而且一行都映射不上。

    所以把所有出现位置都试一遍，取**带值行最多**的那处。
    """
    starts = [p.number for p in doc.pages if title in p.text]
    if not starts:
        return None

    best, best_score = None, -1
    for start in starts:
        tally = 0
        for pg in doc.pages:
            if start <= pg.number <= start + span:
                for t in pg.usable_tables():
                    tally += sum(1 for r in t if len(r) > 2 and (r[2] or "").strip())
        if tally > best_score:
            best, best_score = start, tally

    if best_score <= 0:
        return None

    s = stm.StatementSet(name=title, source=doc.path.name, unit="")
    for pg in doc.pages:
        if best <= pg.number <= best + span:
            for t in pg.usable_tables():
                for row in t:
                    lab = (row[0] or "").replace("\n", " ").strip() if row else ""
                    if not lab:
                        continue
                    f, _ = cn.identify(lab)
                    raw = (row[2] or "").strip()
                    # **OCR 标出来的可疑金额一律不用。**
                    # `？334.719.50` 若交给 `_to_number` 会被剥成 33471950
                    # —— 差 100 倍且不报错。
                    if raw.startswith("？"):
                        v = None
                    else:
                        v = _to_number(raw) if len(row) > 2 else None
                    s.rows.append(stm.StatementRow(lab, v, f, ""))
                    if f and f not in s.fields and v is not None:
                        s.fields[f] = v
    s.unit = ""
    return s


def main() -> int:
    for name, path, cfg, period in CASES:
        p = Path(path)
        print("=" * 78)
        print(f"  {name}")
        print("=" * 78)
        if not p.exists():
            print("  ✗ 文件不存在\n")
            continue

        doc = ip.extract_pdf(p)
        chars = sum(len(pg.text) for pg in doc.pages)
        tabs = sum(len(pg.tables) for pg in doc.pages)
        if doc.ocr_pages and len(doc.ocr_pages) == doc.page_count:
            kind = "**扫描件，全文走 OCR**"
        elif doc.ocr_pages:
            # **不能说成「扫描件」** —— 原生文字层的 PDF 里也常有整页图
            # （封面、组织架构图），那几页会回落到 OCR。
            kind = f"原生文字层（其中 {len(doc.ocr_pages)} 页是整页图，走了 OCR）"
        else:
            kind = "原生文字层"
        print(f"  {doc.page_count} 页 / {chars:,} 字 / {tabs} 个表格块 / {kind}")
        if doc.ocr_pages:
            suspect = 0
            for pg in doc.pages:
                for t in pg.usable_tables():
                    for r in t:
                        suspect += sum(1 for c in r[2:] if str(c).startswith("？"))
            print(f"  ⚠ {len(doc.ocr_pages)} 页走 OCR —— **数字请人工复核**")
            print(f"     {doc.ocr_repaired} 处系统性错标点已按明确判据修复"
                  f"（334.719.50 → 334,719.50）；")
            print(f"     {suspect} 处判据不明确，**已剔除而非猜**。")
        for w in doc.warnings:
            if "OCR" not in w:
                print(f"  ⚠ {w}")

        sets = {}
        for key in ("balance", "income", "cash_flow"):
            title, span = cfg[key]
            sets[key] = build(doc, title, span)
            st = sets[key]
            if st is None:
                print(f"  {key:11} ✗ 定位不到（标题「{title}」没在正文里出现）")
            else:
                mapped = sum(1 for r in st.rows if r.field)
                print(f"  {key:11} {len(st.rows):3} 行，映射上 {mapped} 行")

        S = stm.Statements(
            balance=sets["balance"], income=sets["income"],
            cash_flow=sets["cash_flow"], gaap=cfg["gaap"], scope="合并",
            audited="已审计", period=period,
        )
        print()
        print(f"  勾稽（单位：{cfg['unit']}）")
        for c in S.checks():
            print("    " + c.render(f"（{cfg['unit']}）").replace("\n", "\n    "))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
