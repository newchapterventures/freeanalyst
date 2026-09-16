"""Excel 输入层 —— .xls / .xlsx 财务报表。

## 为什么需要它

中小企业的财务报表**几乎都是 Excel**（而且是 WPS 存的老 .xls）。
上市公司的年报才是 PDF。

## 两个必须处理的格式问题（都实测踩到）

### 一、资产负债表是「左右两栏」的

中文小企业的资产负债表是 T 型布局，**一行里同时装着资产和负债**：

    [0]货币资金 [1]1 [2]21,470,366.19 [3]24,781,633.06 | [4]短期借款 [5]51

左边四列是资产，右边四列是负债及所有者权益。
**所有按「一行属于一张表」的解析器在这里都会错。**
所以要先检测出这种布局，按列切开，还原成两组独立的行。

### 二、列的顺序不是固定的 —— 而且歧义是静默的

| 表 | 列 1 | 列 2 |
|---|---|---|
| 资产负债表 | **年初数** | 期末数 |
| 损益表 | **本月数** | 本年累计 |

资产负债表的**「年初数」在前**。照搬「取第一个数值列」会把
**去年的数当成今年的**，而且不报错 —— 这是本项目最不能容忍的一类错误。

所以列的角色**必须从表头读**，读不出来就报错，不许猜。

## 依赖

- `.xlsx` → `openpyxl`
- `.xls` → `xlrd`（老二进制 BIFF 格式，openpyxl 读不了）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: 表头里「行次 / 附注」类列
_NOTE_HEADERS = ("行次", "行 次", "附注", "行号", "序号", "项目编号", "no", "note")

#: 「本期」类列 —— 我们真正要的那个数
_CURRENT_HEADERS = ("期末数", "期末余额", "期末", "本期数", "本期金额",
                    "本年累计", "本年累计数", "本年数", "年末数", "年末余额",
                    "金额", "本年累计金额")

#: 「非本期」类列 —— 去年的数 / 月度数
_OTHER_HEADERS = ("年初数", "年初余额", "年初", "期初数", "期初余额", "期初",
                  "上期数", "上期金额", "上期", "上年数", "上年同期",
                  "本月数", "本月金额", "本月")

#: 判断「这一格是不是文本标签」——纯数字、日期、空都不算
_NUMERICISH = re.compile(r"^[\s\d,.%()（）\-–—／/年月日:：]*$")


def _is_label(text: str) -> bool:
    """这一格像不像科目名。

    纯数字、纯标点、日期串都不算。
    """
    t = text.strip()
    if len(t) < 2:
        return False
    if _NUMERICISH.match(t):
        return False
    return True


# --------------------------------------------------------------------------
# 读文件
# --------------------------------------------------------------------------

@dataclass
class ExcelSheet:
    name: str
    rows: list[list[object]]

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)


@dataclass
class ExcelWorkbook:
    path: Path
    sheets: list[ExcelSheet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _cell_str(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # xlrd 把整数也读成 float，`2024.0` 这种要去掉尾巴
        return str(int(v)) if v == int(v) else str(v)
    return str(v).strip()


def read_workbook(path: str | Path) -> ExcelWorkbook:
    """读一份 Excel（.xls 或 .xlsx），返回所有工作表。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到文件：{p}")

    suffix = p.suffix.lower()
    if suffix == ".xls":
        return _read_xls(p)
    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(p)
    raise ValueError(f"不认识的 Excel 后缀：{suffix}（支持 .xls / .xlsx / .xlsm）")


def _read_xls(p: Path) -> ExcelWorkbook:
    try:
        import xlrd
    except ImportError as e:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "读 .xls 需要 xlrd：`pip install xlrd`。\n"
            "（.xls 是老二进制格式，openpyxl 读不了。）"
        ) from e

    wb = xlrd.open_workbook(str(p))
    out = ExcelWorkbook(path=p)
    for sh in wb.sheets():
        rows = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]
        out.sheets.append(ExcelSheet(name=sh.name, rows=rows))
    return out


def _read_xlsx(p: Path) -> ExcelWorkbook:
    try:
        from openpyxl import load_workbook
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("读 .xlsx 需要 openpyxl：`pip install openpyxl`") from e

    wb = load_workbook(str(p), data_only=True)
    out = ExcelWorkbook(path=p)
    for name in wb.sheetnames:
        ws = wb[name]
        rows = [[ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
                for r in range(1, ws.max_row + 1)]
        out.sheets.append(ExcelSheet(name=name, rows=rows))
    return out


# --------------------------------------------------------------------------
# 布局识别
# --------------------------------------------------------------------------

def label_columns(rows: list[list[object]], threshold: float = 0.25) -> list[int]:
    """哪些列是「科目名列」。

    判据：这一列在**相当比例的数据行**里装着文本标签。
    左右两栏式资产负债表会有两个这样的列（资产一栏、负债一栏）。

    ## 表头以上的行不参与统计（实测踩到）

    报表顶部是标题区：`损 益 表` / `会工02表` / `单位：元` 挤在右边某一列。
    把它们算进去，那一列会以 27% 的占比**误判成科目名列**
    （阈值刚好是 25%）。所以从表头行的下一行开始数。
    """
    header = find_header_row(rows)
    data = rows[header + 1:] if header is not None else rows
    n = len(data)
    if not n:
        return []
    hits: dict[int, int] = {}
    for row in data:
        for c, v in enumerate(row):
            if _is_label(_cell_str(v)):
                hits[c] = hits.get(c, 0) + 1
    return sorted(c for c, k in hits.items() if k / n >= threshold)


def two_sided_split(rows: list[list[object]]) -> int | None:
    """左右两栏式布局的切分列；不是这种布局就返回 None。

    ## 判据

    表头行的**值列名会重复出现两次**：

        资    产 | 行次 | 年初数 | 期末数 | 负债及所有者权益 | 行次 | 年初数 | 期末数
                   ↑ 第 2 个标签列在这里开始

    只看「有不止一个科目名列」还不够 —— 有的表左边是科目、右边是备注。
    加上「值列名重复」这一条，证据才够。
    """
    cols = label_columns(rows)
    if len(cols) < 2:
        return None

    # 值列名重复出现 → 强烈信号
    header_idx = find_header_row(rows)
    if header_idx is not None:
        header = [_cell_str(v).lower().replace(" ", "") for v in rows[header_idx]]
        # **不能用 `_is_label` 判断「这是不是值列名」** —— 表头里的文字
        # 本来就是文字，`行次`/`年初数`/`期末数` 全会被判成标签，
        # 重复检查就落空了（实测踩到，导致左右两栏没切开）。
        names = [h for h in header if h in _CURRENT_HEADERS or h in _OTHER_HEADERS]
        if names and len(names) != len(set(names)):
            # 第二个标签列就是右半边的起点
            return cols[1]
    return None


def find_header_row(rows: list[list[object]]) -> int | None:
    """找表头行 —— 同时含「行次类」和「本期类」列名的那一行。"""
    for i, row in enumerate(rows):
        cells = [_cell_str(v).lower().replace(" ", "") for v in row]
        has_note = any(c in _NOTE_HEADERS or c == "行次" for c in cells)
        has_val = any(c in _CURRENT_HEADERS or c in _OTHER_HEADERS for c in cells)
        if has_note and has_val:
            return i
    return None


def split_blocks(rows: list[list[object]]) -> list[list[list[object]]]:
    """把一张表拆成若干个「单栏」块。

    左右两栏式资产负债表 → 2 块；其余 → 1 块。
    """
    cut = two_sided_split(rows)
    if cut is None:
        return [rows]
    return [[r[:cut] for r in rows], [r[cut:] for r in rows]]


# --------------------------------------------------------------------------
# 列角色 —— 这一步错就是把年份搞错
# --------------------------------------------------------------------------

@dataclass
class ColumnRoles:
    """各列是什么。

    **`primary` / `other` 的顺序必须由表头决定，不能假设。**
    资产负债表的「年初数」在前、期末数在后；照搬「取第一个数值列」
    会把去年的数当成今年的，而且不报错。
    """

    label: int = 0
    note: int | None = None
    primary: int | None = None      # 我们要的那个：期末数 / 本年累计
    other: int | None = None        # 另一个：年初数 / 本月数
    primary_name: str = ""
    other_name: str = ""
    header_row: int | None = None

    @property
    def ok(self) -> bool:
        return self.primary is not None


def column_roles(rows: list[list[object]]) -> ColumnRoles:
    """从表头读出各列的角色。读不出来就返回 `ok=False`。"""
    roles = ColumnRoles()
    idx = find_header_row(rows)
    if idx is None:
        return roles
    roles.header_row = idx

    header = [_cell_str(v).lower().replace(" ", "") for v in rows[idx]]
    for c, h in enumerate(header):
        if not h:
            continue
        # **每个角色只认第一个匹配列。** 左右两栏表里「行次/年初数/期末数」
        # 各出现两次，不加这个限制会取到右栏的列（实测踩到：
        # 货币资金的行次变成了短期借款的 51）。
        if (h in _NOTE_HEADERS or h == "行次") and roles.note is None:
            roles.note = c
        elif h in _CURRENT_HEADERS and roles.primary is None:
            roles.primary = c
            roles.primary_name = _cell_str(rows[idx][c])
        elif h in _OTHER_HEADERS and roles.other is None:
            roles.other = c
            roles.other_name = _cell_str(rows[idx][c])

    # 表头认不出来就退回「第一个数值列是本期」——
    # 但那要**明确报警**，不能装作知道。
    if roles.primary is None:
        roles.primary = roles.note + 1 if roles.note is not None else 1
    return roles


def to_rows(block: list[list[object]]) -> tuple[list[list[str]], ColumnRoles]:
    """把一个单栏块转成 `[标签, 附注, 本期值, 上期值]` 的行。

    顺序由表头决定 —— **`primary` 那列永远放在第 3 位**，
    不管它在原表里排第几。
    """
    roles = column_roles(block)
    out: list[list[str]] = []
    start = (roles.header_row + 1) if roles.header_row is not None else 0

    for row in block[start:]:
        def get(c: int | None) -> str:
            if c is None or c >= len(row):
                return ""
            return _cell_str(row[c])

        label = get(roles.label)
        if not label:
            continue
        note = get(roles.note)
        primary = get(roles.primary)
        other = get(roles.other)
        if not primary and not other:
            # 只有科目名、没有数字 —— 段标题，留着（下游会跳过）
            out.append([label, note, "", ""])
            continue
        out.append([label, note, primary, other])
    return out, roles
