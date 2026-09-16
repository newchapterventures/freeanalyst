"""输入层验收报告 —— 拿**真实材料**跑，逐份看勾稽平不平。

跑法：`python3 -m financials.verify_reports`

## ⚠️ 材料路径是私有的，不进仓库

这份脚本在公开仓库里，但**它跑的是私密材料**。所以路径不写死在这里，
而是从 `root/verify-cases.json` 读（`root/` 已 gitignore）。

`root/verify-cases.json` 长这样：

    [
      {"title": "某 A 股年报（原生文字层）",
       "path": "/path/to/report.pdf",
       "unit": "元",
       "titles": {"balance": "合并资产负债表", "income": "合并利润表",
                  "cash_flow": "合并现金流量表"},
       "span": {"balance": 3, "income": 3, "cash_flow": 3},
       "gaap": "CAS", "period": "2025-12-31"},
      ...
    ]

**为什么走文件而不是写死** —— 之前写死过，结果把标的公司名和用户的本地绝对
路径一起提交到了公开仓库。保密边界是「文件保密、工具不保密」，
标的公司名本身也是机密（把「某人在看 X 公司」公开出去等于公开 deal 名单）。
"""

from __future__ import annotations

import json
import logging
import sys

logging.disable(logging.WARNING)

from pathlib import Path

from ingest import pdf as ip
from ingest.html import _to_number
from financials import canonical as cn
from financials import statements as stm

#: 私有配置的位置（`root/` 在 .gitignore 里）
CONFIG = Path(__file__).resolve().parent.parent / "root" / "verify-cases.json"


def load_cases() -> list[tuple]:
    """从私有配置读材料清单。没有配置就明说，不要假装跑过了。"""
    if not CONFIG.exists():
        print(f"  ⚠ 找不到私有材料清单：{CONFIG}")
        print("     这是**故意**的 —— 材料路径不进公开仓库。")
        print("     照文件顶部注释里的格式建一个即可。")
        return []
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    out = []
    for c in raw:
        cfg = {"unit": c.get("unit", ""), "gaap": c.get("gaap", "")}
        for kind, title in (c.get("titles") or {}).items():
            cfg[kind] = (title, (c.get("span") or {}).get(kind, 3))
        out.append((c["title"], c["path"], cfg, c.get("period", "")))
    return out


CASES = load_cases()


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
