"""HTML 抽取 —— **零依赖**，用标准库 `html.parser`。

## 为什么把 HTML 和 PDF 分开写

PDF 要跟字体子集、压缩流、没有结构的坐标搏斗（见 `ingest/pdf.py` 里那些坑）。
HTML 是**有结构的**：`<table>` 就是表格，`<th>` 就是表头。

同一个项目里两者的难度差一个量级。所以：

- HTML：标准库搞定，零依赖
- PDF：需要 pdfplumber，做成可选依赖

## SEC 申报文件是 HTML，这是个白送的礼物

真实尽调材料的形态：CIM 是 PDF，**但上市公司申报文件全是 HTML**（SEC 不发 PDF 版）。
所以支持 HTML 等于把「上市公司财报」这条线整个打通。

而且 SEC 渲染的 XBRL 报表 HTML 里**内嵌了官方科目标签**：

```html
onclick="top.Show.showAR( this, 'defref_us-gaap_CashAndCashEquivalentsAtCarryingValue', window )"
```

这一行把 `Cash and cash equivalents` 这个行名和
`us-gaap:CashAndCashEquivalentsAtCarryingValue` 这个**标准标签**连了起来。

本模块把标签一起抽出来（`HtmlRow.xbrl_tag`）—— 于是「三张表科目映射」
在 SEC 文件上变成了**查表**而不是**猜**。

中文字段的科目映射是另一回事，那个还得自己建（见 docs/valuation-spec.md §4）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .normalize import normalize_text

#: 从 onclick 里抽 XBRL 标签：defref_us-gaap_FooBar  →  us-gaap:FooBar
_XBRL_TAG = re.compile(r"defref_([A-Za-z0-9-]+)_([A-Za-z0-9_]+)")

#: 这些标签里的内容不是正文
_SKIP_TAGS = {"script", "style", "head", "title", "meta", "link"}

#: **自闭合标签，没有结束标签。**
#
# 踩过的坑：`<link rel="stylesheet" href="report.css">` 没有 `</link>`。
# 如果按「遇到开始标签 +1、遇到结束标签 -1」计数，skip 计数只增不减，
# 整个文档的正文和表格**全被跳过**，最后返回 0 张表 —— 而且不报错。
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

#: 块级标签 —— 前后要断行，否则段落会粘成一片
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "section", "article", "header", "footer", "blockquote", "hr",
}


@dataclass
class HtmlRow:
    """表格里的一行。"""

    label: str
    cells: list[str]
    #: 官方 XBRL 标签（SEC 申报文件才有），形如 `us-gaap:CashAndCashEquivalents...`
    xbrl_tag: str | None = None
    is_section_header: bool = False

    def numeric_cells(self) -> list[float | None]:
        return [_to_number(c) for c in self.cells]


@dataclass
class HtmlTable:
    """一张表。"""

    header: list[str]
    rows: list[HtmlRow] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.header[0] if self.header else ""

    def markdown(self) -> str:
        out: list[str] = []
        if self.header:
            out.append("| " + " | ".join(self.header) + " |")
        for r in self.rows:
            out.append("| " + " | ".join([r.label, *r.cells]) + " |")
        return "\n".join(out)


@dataclass
class HtmlDocument:
    path: Path
    text: str
    tables: list[HtmlTable]
    title: str = ""

    def table_with(self, *keywords: str) -> HtmlTable | None:
        """按标题关键词找表。"""
        for t in self.tables:
            if all(k.lower() in t.title.lower() for k in keywords):
                return t
        return None

    def to_text(self) -> str:
        """给索引用的整篇文本：标题 + 正文 + 全部表格（markdown）。

        **表格必须进文本。** 财务报表的数字全在表里，
        只索引正文等于把最重要的内容丢掉。

        表头带上标题和 XBRL 标签 —— 标签是官方科目名，
        检索「总资产」时能靠标签里的 `Assets` 命中也算意外收获。
        """
        parts: list[str] = []
        if self.title:
            parts.append(f"【{self.title}】")
        if self.text.strip():
            parts.append(self.text.strip())

        for i, t in enumerate(self.tables, 1):
            head = t.title or f"表 {i}"
            parts.append(f"\n【表 {i}】{head}\n{t.markdown()}")

        return "\n\n".join(parts)


#: 只由这些符号构成的单元格 —— 它们是数字的碎片，不是独立的一列
_FRAGMENT_CLOSE = re.compile(r"^[)）]+$")
_FRAGMENT_SYMBOL = re.compile(r"^[$¥€£(（]+$")


def _normalize_cells(cells: list[str]) -> list[str]:
    """把 SEC 拆散的数字拼回一格，**保持列位置**。

    ## 为什么必须做（和 `$ (102,777)` 是同一类错误）

    SEC 的 HTML 为了对齐，会把一个数字拆进多个 `<td>`：

        <td colspan="2">(3,552</td><td>)</td>      负数
        <td>$</td><td>301,320</td>                  货币符号

    不拼回去的话，`_to_number("(3,552")` 会因为不以 `)` 结尾而判不出括号，
    剥掉 `(` 之后得到 **正** 3552 —— **亏损又变成盈利。**

    ## 合并规则

    - `$`、`(` 这类前缀碎片 → 往后找第一个有内容的格，拼在它前面
    - `)` 这类后缀碎片 → 往前找第一个有内容的格，拼在它后面
    - **结果留在这一组碎片的第一个位置**，其他位置清空，列数不变
    """
    out = list(cells)
    i = 0
    while i < len(out):
        s = out[i].strip()
        if not s:
            i += 1
            continue

        # 后缀碎片：往前并
        if _FRAGMENT_CLOSE.match(s):
            j = i - 1
            while j >= 0 and not out[j].strip():
                j -= 1
            if j >= 0:
                out[j] = out[j].strip() + s
                out[i] = ""
            i += 1
            continue

        # 前缀碎片：往后并，但结果留在这里
        if _FRAGMENT_SYMBOL.match(s):
            acc = s
            j = i + 1
            while j < len(out):
                nxt = out[j].strip()
                if not nxt:
                    j += 1
                    continue
                if _FRAGMENT_CLOSE.match(nxt) or _FRAGMENT_SYMBOL.match(nxt):
                    acc += nxt
                    out[j] = ""
                    j += 1
                    continue
                acc += nxt
                out[j] = ""
                break
            out[i] = acc
            i = j
            continue

        i += 1
    return out


def _to_number(cell: str | None) -> float | None:
    """把单元格文本转成数字。

    要处理：千分位逗号、货币符号、括号表示负数、破折号表示零或缺失。

    ## 括号负数 —— 这里踩过一个会翻转盈亏的 bug

    **必须先剥掉货币符号，再判断括号。** 原来直接判 `startswith("(")`，
    而财务报表里的写法是 `$ (102,777)` —— **以 `$` 开头**，
    于是括号判断失败，后面 `[^\\d.,\\-]` 又把括号当普通字符剥掉，
    负数就变成了正数。

        19  Net income (loss) attributable...  '(102,777)'   → -102777  ✓
        21  Net income (loss) attributable...  '$ (102,777)' →  102777  ✗

    **这个 bug 会把亏损静默变成盈利。** 一家亏 1.03 亿的公司显示成赚 1.03 亿，
    而且没有任何报错。是靠跟官方 XBRL 对账才发现的。
    """
    if cell is None:
        # PDF 抽出来的表格里空单元格常是 None 而不是 ""。
        # 让调用方每次自己判断会到处漏。
        return None
    s = cell.strip()
    if not s:
        return None

    # 破折号 / em dash 表示零或"无"
    if s in {"—", "–", "-", "–", "—", "N/A", "n/a"}:
        return None

    # **日期不是金额。** A 股年报跨页拼接时表头会重复出现，
    # 「2025年12月31日」会被剥成 20251231 —— 变成一个看着像金额的假数。
    if re.search(r"[年月日]", s) or re.search(r"\d{4}-\d{2}-\d{2}", s):
        return None
    # 纯单位行
    if s in {"万元", "元", "千元", "人民币元", "美元", "千美元"}:
        return None

    # **先剥货币符号和空白** —— 括号判断必须在之后做
    s = re.sub(r"[\s$¥€£₩₹]", "", s)

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]

    s = re.sub(r"[^\d.,\-]", "", s)          # 去掉残留的百分号等
    s = s.replace(",", "")
    if not s or s in {"-", ".", "-."}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if negative else v


class _Extractor(HTMLParser):
    """一遍扫完，同时收正文和表格。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.tables: list[HtmlTable] = []
        self.title = ""

        self._skip_depth = 0
        self._table_depth = 0
        self._cur_table: HtmlTable | None = None
        self._cur_row: HtmlRow | None = None
        self._cell_text: list[str] = []
        self._in_cell = False
        self._in_title = False

        # rowspan 补位用：记录哪些列在未来几行里被占着
        #   _pending[row_offset][col] = 剩余行数
        self._pending: dict[int, dict[int, int]] = {}
        self._row_index = 0

    # ---------- 正文 ----------

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return

        # **XBRL 标签挂在单元格内部的 `<a>` 上，不在 `<td>` 上。**
        # 原实现在 `<td>` 的属性里找，一行都找不到 —— 所以这里扫单元格内所有标签。
        if self._in_cell:
            for attr_val in dict(attrs).values():
                m = _XBRL_TAG.search(attr_val or "")
                if m:
                    self._cur_cell_tag = f"{m.group(1)}:{m.group(2)}"
                    break

        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._cur_table = HtmlTable(header=[])
                self._pending = {}
                self._row_index = 0
            return

        if tag == "tr" and self._cur_table is not None:
            self._cur_row = HtmlRow(label="", cells=[])
            return

        if tag in ("td", "th") and self._cur_row is not None:
            self._in_cell = True
            self._cell_text = []
            attrs_d = dict(attrs)
            colspan = int(attrs_d.get("colspan", 1) or 1)
            rowspan = int(attrs_d.get("rowspan", 1) or 1)
            self._cur_colspan = colspan
            self._cur_rowspan = rowspan
            self._cur_cell_tag = None
            for attr_val in attrs_d.values():
                m = _XBRL_TAG.search(attr_val or "")
                if m:
                    self._cur_cell_tag = f"{m.group(1)}:{m.group(2)}"
                    break
            return

        if tag in _BLOCK_TAGS and self._table_depth == 0:
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return

        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
            if self._table_depth == 0:
                self._finish_table()
            return

        if tag == "tr" and self._cur_row is not None:
            self._finish_row()
            return

        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._place_cell("".join(self._cell_text))
            return

        if tag in _BLOCK_TAGS and self._table_depth == 0:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth:
            return
        if self._in_cell:
            self._cell_text.append(data)
        elif self._table_depth == 0:
            self.text_parts.append(data)

    # ---------- 表格装配 ----------

    def _place_cell(self, raw: str) -> None:
        """把单元格放进网格（跳过被 rowspan 占掉的位置）。"""
        assert self._cur_row is not None
        row = self._cur_row

        # 跳过左侧被上面行 rowspan 占住的列
        occupied = self._pending.get(0, {})
        col = 0
        while occupied.get(col, 0) > 0:
            col += 1

        tag = getattr(self, "_cur_cell_tag", None)

        value = re.sub(r"\s+", " ", raw).strip()

        span = getattr(self, "_cur_colspan", 1)
        rowspan = getattr(self, "_cur_rowspan", 1)

        # 第一个格子当行名（含 XBRL 标签）
        if not row.label and not row.cells:
            row.label = value
            row.xbrl_tag = tag
        else:
            row.cells.extend([value] + [""] * (span - 1))
        # 登记 rowspan，供后续行跳过这些列
        if rowspan > 1:
            used = max(1, span)
            for r in range(1, rowspan):
                for c in range(col, col + used):
                    self._pending.setdefault(r, {})[c] = 1

    def _finish_row(self) -> None:
        assert self._cur_row is not None
        row = self._cur_row
        # 把 SEC 拆散的数字碎片拼回去（`(3,552` + `)` → `(3,552)`）
        row.cells = _normalize_cells(row.cells)

        collapsed = {k: v for k, v in self._pending.items() if k > 0}
        self._pending = {
            k - 1: v for k, v in collapsed.items() if k - 1 >= 0
        }
        self._row_index += 1

        if self._cur_table is not None:
            if not self._cur_table.header:
                # 第一行是表头（SEC 报表就是这样：第一格是标题，后面是日期列）
                self._cur_table.header = [row.label, *row.cells]
            elif row.label or any(row.cells):
                self._cur_table.rows.append(row)
        self._cur_row = None

    def _finish_table(self) -> None:
        if self._cur_table is not None:
            if self._cur_table.header and self._cur_table.rows:
                self.tables.append(self._cur_table)
            self._cur_table = None


def extract_html(path: Path) -> HtmlDocument:
    """抽一份 HTML 的正文和表格。"""
    raw = path.read_text(encoding="utf-8", errors="replace")

    # SEC 文件外面套了一层 SGML 风格的信封，只取 <TEXT> 里的部分
    m = re.search(r"<TEXT>(.*?)</TEXT>", raw, re.S | re.I)
    if m:
        raw = m.group(1)

    ex = _Extractor()
    ex.feed(raw)

    text = normalize_text("".join(ex.text_parts))
    # 压掉多余空行，但保留段落分隔
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)

    tables = []
    for t in ex.tables:
        hdr = [normalize_text(h) for h in t.header]
        rows = [
            HtmlRow(
                label=normalize_text(r.label),
                cells=[normalize_text(c) for c in r.cells],
                xbrl_tag=r.xbrl_tag,
            )
            for r in t.rows
        ]
        tables.append(HtmlTable(header=hdr, rows=rows))

    return HtmlDocument(path=path, text=text, tables=tables,
                        title=normalize_text(ex.title) or path.stem)
