"""公开域闸门 —— 允许联网，但机密数据出不去。

## 为什么需要这个

`guard.py` 的规则是"这个程序不出网"，证明方式一句话：审计日志里写
`外部主机 无`。

但产品需要联网（web search、SEC EDGAR、AKShare 抓可比公司）。于是规则变成
"允许联网，但**文件内容永不出境**"——**而这条比上一条难证明得多。**

## 为什么不用内容过滤

因为做不到。你无法判断一段出境文本里有没有夹带机密：

> "标的企业A、收入1.83亿、增速17.9%、行业只有三家可比"
>
> 这组数字本身就是指纹，把公司名替换成"A"没有用。

## 隔离靠三条，都是确定性的

1. **白名单主机** —— 只允许公开数据源，不允许任意 URL
2. **结构化参数** —— 出境参数只能是代码 / 日期 / 指标名这类短值，拒绝自由文本
3. **代码路径隔离** —— 这个模块**不 import retrieval / 不读 corpus / 不读 root**

**但要说清楚**：第 1、2 条是**防误伤**（意外夹带），不是防对手。真正的保证是
第 3 条——**公开域的代码路径根本拿不到机密数据**。格式检查是纵深防御，不是主防线。

## 审计日志可以公开给人看

因为记下的只有公开查询词，没有机密。这才是"数据不出境"在新架构下的
可证明形式：不是承诺，是一份**你可以直接展示给 LP 看的日志**。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 白名单：只允许这些主机
#
# 新增数据源时必须显式加进来 —— 不允许通配。
# ---------------------------------------------------------------------------

DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset({
    # 美国证监会（免费、无 key、官方结构化数据）
    "data.sec.gov",
    "efts.sec.gov",
    "www.sec.gov",
})

AUDIT_PATH = Path(
    os.environ.get(
        "FREEANALYST_PUBLIC_AUDIT",
        Path(__file__).resolve().parent / "audits" / "egress-public.jsonl",
    )
)

# 单个参数值的长度上限。语料片段通常远超这个长度。
MAX_VALUE_LEN = 64

# 本地引用模式 —— 这些是机密域的内部编号，绝不该出现在出境请求里
_LOCAL_REF_PATTERNS = (
    re.compile(r"\[S\d+\]"),      # 检索片段的编号
    re.compile(r"#p\d+"),         # 段落引用
    re.compile(r"\bchunk[_-]?id\b", re.I),
)

# 换行与超长连续文本是语料特征，不是查询参数特征
_WHITESPACE = re.compile(r"[\r\n\t]")

# 连续 CJK 字符超过这个数就拒绝 —— 公司名/行业名是短词，成段中文是材料
MAX_CJK_RUN = 20
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{%d,}" % (MAX_CJK_RUN + 1))


class PublicEgressBlocked(RuntimeError):
    """出境请求被公开域闸门拦下。"""


@dataclass
class PublicQuery:
    """一个出境请求的参数。构造时就校验，不合格直接抛错。

    只接受标量和短列表。嵌套结构、长文本、本地引用一律拒绝。
    """

    params: dict[str, Any]
    purpose: str = ""
    extra_hosts: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for key, value in self.params.items():
            self._check_value(key, value)

    @staticmethod
    def _check_value(key: str, value: Any) -> None:
        if isinstance(value, (list, tuple)):
            for v in value:
                PublicQuery._check_value(key, v)
            return

        if not isinstance(value, (str, int, float)):
            raise PublicEgressBlocked(
                f"参数 {key!r} 类型为 {type(value).__name__}，"
                f"只允许 str / int / float（结构化参数）"
            )

        if isinstance(value, (int, float)):
            return

        text: str = value
        if len(text) > MAX_VALUE_LEN:
            raise PublicEgressBlocked(
                f"参数 {key!r} 长度 {len(text)} 超过上限 {MAX_VALUE_LEN} —— "
                f"出境参数只能是短结构化值，不能是文本片段"
            )
        if _WHITESPACE.search(text):
            raise PublicEgressBlocked(f"参数 {key!r} 含换行或制表符 —— 这是材料文本的特征")
        for pat in _LOCAL_REF_PATTERNS:
            if pat.search(text):
                raise PublicEgressBlocked(
                    f"参数 {key!r} 含本地引用「{pat.pattern}」—— 机密域内部编号不得出境"
                )
        m = _CJK_RUN.search(text)
        if m:
            raise PublicEgressBlocked(
                f"参数 {key!r} 含 {len(m.group(0))} 个连续中文字符 —— "
                f"超过 {MAX_CJK_RUN} 字按材料文本处理，拒绝出境"
            )


def _audit(event: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def allowed_hosts(extra: frozenset[str] = frozenset()) -> frozenset[str]:
    """当前白名单。环境变量 FREEANALYST_EXTRA_HOSTS 可追加（逗号分隔）。"""
    env = os.environ.get("FREEANALYST_EXTRA_HOSTS", "")
    from_env = frozenset(h.strip() for h in env.split(",") if h.strip())
    return DEFAULT_ALLOWED_HOSTS | from_env | extra


def guarded_get(
    url: str,
    query: PublicQuery,
    timeout: int = 30,
    headers: dict[str, str] | None = None,
) -> bytes:
    """公开域唯一的出网函数。

    与 `guard.guarded_request` 的区别：这里**允许外部主机**，但只允许白名单内的，
    且参数必须通过结构化校验。
    """
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    allow = allowed_hosts(query.extra_hosts)
    ok_host = host in allow

    _audit({
        "event": "public_egress",
        "domain": "public",
        "decision": "ALLOW" if ok_host else "BLOCK",
        "host": host,
        "path": parsed.path,
        "params": query.params,          # 只有公开查询词，可公开审计
        "purpose": query.purpose,
    })

    if not ok_host:
        raise PublicEgressBlocked(
            f"主机 {host} 不在公开域白名单内。"
            f"当前白名单：{sorted(allow)}。"
            f"新增数据源时必须显式加进来——不允许通配。"
        )

    full = url
    if query.params:
        qs = urllib.parse.urlencode(query.params, doseq=True)
        full = f"{url}{'&' if '?' in url else '?'}{qs}"

    req = urllib.request.Request(full, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def public_audit_summary() -> dict:
    """公开域审计汇总。

    这份日志与机密域的日志分开：机密域应显示「外部主机 无」，
    公开域会显示被允许的数据源 —— 但**参数列里只有公开查询词**。
    """
    if not AUDIT_PATH.exists():
        return {"total": 0, "allowed": 0, "blocked": 0, "hosts": [], "path": str(AUDIT_PATH)}

    total = allowed = blocked = 0
    hosts: set[str] = set()
    with AUDIT_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "public_egress":
                continue
            total += 1
            if rec.get("decision") == "ALLOW":
                allowed += 1
                hosts.add(rec.get("host", "?"))
            else:
                blocked += 1

    return {"total": total, "allowed": allowed, "blocked": blocked,
            "hosts": sorted(hosts), "path": str(AUDIT_PATH)}
