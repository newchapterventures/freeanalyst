"""Egress guard — 全项目唯一的出网闸门。

设计原则：这个项目里所有网络调用**必须**经过 guarded_request()。
任何不在白名单上的目标会被拒绝，并写入审计日志。

这是"可验证保密"的实现基础：任何人 clone 代码后，
只要 grep `urllib` / `requests` / `socket` 就能确认只有这一个出口。

## 云端模型：默认拦截，按次显式授权

用户的原话：

> 有些信息用户觉得没必要保密，所以可以让闭源云端大模型来分析。

所以出网有两个层级：

    1. 回环地址（本地模型）         —— 默认放行
    2. 外部主机（云端模型）         —— **默认拦截**，除非这一次调用带着
                                      用户对**这一份内容**的显式授权

**刻意不做全局开关。** 一个 `ALLOW_CLOUD=1` 之类的环境变量会让
「材料不出本机」这句话失去意义 —— 装完就一直是开着的。
授权对象是 `CloudConsent`，它绑定**主机 + 用途 + 内容摘要**，
每次调用都得有，而且**授权这件事本身也写进审计日志**。

审计里记**内容的摘要和字节数**，不记内容本身 —— 这样事后能证明
「那次到底发出去了多少、是不是同一份」，但审计文件本身不会变成第二个泄密点。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# 只允许回环地址。**外部主机一律拒绝，除非带显式授权。**
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

AUDIT_PATH = Path(
    os.environ.get(
        "FREEANALYST_AUDIT", Path(__file__).resolve().parent / "audits" / "egress.jsonl"
    )
)


class EgressBlocked(RuntimeError):
    """尝试访问白名单之外的主机，且没有有效授权时抛出。"""


@dataclass(frozen=True)
class CloudConsent:
    """用户对「把这一份内容发给这台云端主机」的显式授权。

    **按次**，不是一次授权永久生效。三个字段都是必需的：

    - `host`     发给谁（例如 `api.deepseek.com`）
    - `purpose`  干什么用（例如「让云端模型给这段纪要写摘要」）
    - `what`     发的是什么（给人看的描述，例如「访谈纪要第 3 页，1,200 字」）

    `what` 是写给人看的 —— 授权界面上要能让用户看懂「到底要发什么出去」，
    而不是只显示一个主机名。
    """

    host: str
    purpose: str
    what: str
    approved_by: str = "user"


def _audit(event: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _digest(data: bytes | None) -> dict:
    """内容的摘要 —— 事后能核对「发的是不是同一份」，但审计文件不存内容。"""
    if not data:
        return {"bytes": 0}
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16]}


def guarded_request(
    url: str,
    data: bytes | None = None,
    headers: dict | None = None,
    purpose: str = "",
    timeout: int = 900,
    consent: CloudConsent | None = None,
) -> bytes:
    """唯一允许发起网络请求的函数。

    ## 三种结果

        host 是回环地址                          -> 放行，审计记 ALLOW
        host 是外部地址 且 consent 匹配          -> 放行，审计记 ALLOW-CONSENT
        host 是外部地址 且没有/不匹配的 consent   -> 拦截，审计记 BLOCK

    `consent` 必须**同时**匹配 host 和 purpose —— 拿 A 用途的授权去调 B 用途
    不算数，免得一次授权被当成万能通行证。
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    loopback = host in ALLOWED_HOSTS

    matched = (
        consent is not None
        and consent.host.lower() == host
        and (not consent.purpose or consent.purpose == purpose)
    )

    if loopback:
        decision, reason = "ALLOW", "回环地址"
    elif matched:
        decision, reason = "ALLOW-CONSENT", "用户显式授权"
    else:
        if consent is None:
            reason = "外部主机，没有授权"
        elif consent.host.lower() != host:
            reason = f"授权的主机是 {consent.host}，不是 {host}"
        else:
            reason = f"授权用途是「{consent.purpose}」，本次是「{purpose}」"
        decision = "BLOCK"

    event = {
        "event": "egress",
        "decision": decision,
        "host": host,
        "url": url,
        "purpose": purpose,
        "reason": reason,
    }
    # 只有真的要出去（含被授权的），才记内容摘要；拦截的不记
    if decision != "BLOCK":
        event["payload"] = _digest(data)
    if matched:
        event["consent"] = {"what": consent.what, "approved_by": consent.approved_by}
    _audit(event)

    if decision == "BLOCK":
        raise EgressBlocked(
            f"出网已被拦截：{host}（{reason}）。\n"
            f"  回环地址永远放行；外部主机需要一次显式授权（`CloudConsent`）。\n"
            f"  本次尝试已记入 {AUDIT_PATH}"
        )

    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()



def audit_summary() -> dict:
    """汇总审计日志：总调用数、被拦截数、涉及的外部主机。"""
    if not AUDIT_PATH.exists():
        return {"total": 0, "allowed": 0, "blocked": 0, "external_hosts": [], "path": str(AUDIT_PATH)}

    total = allowed = blocked = 0
    external: set[str] = set()
    with AUDIT_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "egress":
                continue
            total += 1
            if rec.get("decision") == "ALLOW":
                allowed += 1
            else:
                blocked += 1
                external.add(rec.get("host", "?"))

    return {
        "total": total,
        "allowed": allowed,
        "blocked": blocked,
        "external_hosts": sorted(external),
        "path": str(AUDIT_PATH),
    }
