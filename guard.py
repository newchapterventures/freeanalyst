"""Egress guard — 全项目唯一的出网闸门。

设计原则：这个项目里所有网络调用**必须**经过 guarded_request()。
任何不在白名单上的目标会被拒绝，并写入审计日志。

这是"可验证保密"的实现基础：任何人 clone 代码后，
只要 grep `urllib` / `requests` / `socket` 就能确认只有这一个出口。
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

# 只允许回环地址。任何外部主机一律拒绝。
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

AUDIT_PATH = Path(
    os.environ.get(
        "FREEANALYST_AUDIT", Path(__file__).resolve().parent / "audits" / "egress.jsonl"
    )
)


class EgressBlocked(RuntimeError):
    """尝试访问白名单之外的主机时抛出。"""


def _audit(event: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def guarded_request(
    url: str,
    data: bytes | None = None,
    headers: dict | None = None,
    purpose: str = "",
    timeout: int = 900,
) -> bytes:
    """唯一允许发起网络请求的函数。

    host 不在 ALLOWED_HOSTS 内 -> 审计记为 BLOCK 并抛 EgressBlocked。
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    allowed = host in ALLOWED_HOSTS

    _audit(
        {
            "event": "egress",
            "decision": "ALLOW" if allowed else "BLOCK",
            "host": host,
            "url": url,
            "purpose": purpose,
        }
    )

    if not allowed:
        raise EgressBlocked(
            f"出网已被拦截：{host}（允许的目标只有 {sorted(ALLOWED_HOSTS)}）。"
            f"本次尝试已记入 {AUDIT_PATH}"
        )

    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
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
