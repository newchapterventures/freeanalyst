"""查"当前该装哪个模型" —— 让推荐清单**会过期这件事由代码承担**。

## 为什么要这个模块

我们差点踩进一个坑：README 里写 `ollama pull qwen2.5-coder:7b`，那是一个**五个月前**的模型，
实测质量门槛只有 2/5。清单写死在文档里，三个月后就会变成"推荐一个过时的东西"。

所以：**候选代次放在代码里，具体型号与体积问官方 registry 要**（并带日期戳的缓存）。

## 出网这件事

`registry.ollama.ai` 是外部主机 —— 按这个工具一贯的规矩，`guard` 默认**拦截**。
要查就得按次授权：绑定 **主机 + 用途 + 这次发什么**。
这里发出去的只有**模型家族名**（`qwen3.5`），**不含任何材料内容**，也没有公司名 ——
授权照样进审计，因为"发了什么"要能被查。

查不到（离线、被拦、官方改版）就**明确说查不到**，回退到本地清单并标明日期 ——
不编一个体积数字出来。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: 查到的体积缓存（官方 registry 的响应）。带日期戳，过期就重新查、查不到就用旧的但标明日期
CACHE = ROOT / "bench" / "registry-cache.json"
REGISTRY = "https://registry.ollama.ai"
HOST = "registry.ollama.ai"
#: 缓存多久算新鲜。模型体积基本不变，缓存久一点没关系 —— 反正会显示日期
CACHE_TTL_DAYS = 14

#: 每个内存档位的候选（**按代次从新到旧**）。第一个查得到就用第一个。
#: 加新代次 = 在对应的档位列表前面加一行。
CANDIDATES: dict[str, list[str]] = {
    "8": ["qwen3.5:4b", "qwen3:4b", "gemma3:4b", "llama3.2:3b"],
    "16": ["qwen3.5:9b", "qwen3:8b", "llama3.1:8b", "qwen2.5:7b"],
    "24": ["qwen3.5:9b", "qwen3:14b", "gemma3:12b"],
    "32": ["qwen3.6:35b-a3b", "qwen3.5:27b", "qwen3:30b-a3b"],
}


def tier_for(ram_gb: float) -> str:
    """内存 → 档位键。探不到内存按最保守的 8GB 档（宁小不大）。"""
    if not ram_gb:
        return "8"
    for floor in ("32", "24", "16"):
        if ram_gb >= float(floor):
            return floor
    return "8"


def manifest_size_gb(family: str, tag: str, timeout: int = 20,
                     consent=None) -> float | None:
    """问官方 registry 要这个 tag 的体积（各层加起来）。

    走 `guard.guarded_request` —— 外部主机**必须带按次授权**，否则被拦。
    """
    from guard import guarded_request

    url = f"{REGISTRY}/v2/library/{family}/manifests/{tag}"
    try:
        raw = guarded_request(
            url, purpose="查模型体积与当前代次（只发模型名，不含材料）",
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            timeout=timeout, consent=consent)
        d = json.loads(raw)
    except Exception:                                       # noqa: BLE001
        return None
    total = sum(int(l.get("size") or 0)
                for l in (d.get("layers") or d.get("manifests") or []))
    return round(total / 1024 ** 3, 1) if total else None


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def cached(model: str) -> tuple[float | None, str]:
    """缓存里的体积与查询日期。查过就返回（哪怕过期也不删，页面会标明日期）。"""
    d = _load_cache().get(model) or {}
    return d.get("gb"), (d.get("checked") or "")


def check_candidates(ram_gb: float = 0.0, consent=None,
                     refresh: bool = False) -> dict:
    """按内存档位查一遍候选，返回**第一个查得到的**（含体积与来源说明）。

    返回里一定带 `source`：`registry`（联网查到的）还是 `offline`（没查成，回退清单）。
    页面照实显示 —— 不把"回退清单"说成"最新"。
    """
    tier = tier_for(ram_gb)
    cands = CANDIDATES[tier]
    cache = _load_cache()
    checked_today = time.strftime("%Y-%m-%d")
    out = {"tier": tier, "candidates": cands, "picked": "", "gb": None,
           "source": "offline", "checked": "", "why": ""}

    if not refresh:
        for m in cands:                                     # 先用缓存（不联网）
            gb, when = cached(m)
            if gb:
                out.update({"picked": m, "gb": gb, "source": "cache",
                            "checked": when,
                            "why": f"用 {when} 查到的体积缓存（{gb}GB）"})
                return out

    for m in cands:
        family, _, tag = m.partition(":")
        gb = manifest_size_gb(family, tag or "latest", consent=consent)
        if gb:
            cache[m] = {"gb": gb, "checked": checked_today}
            try:
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            except Exception:                               # noqa: BLE001
                pass
            out.update({"picked": m, "gb": gb, "source": "registry",
                        "checked": checked_today,
                        "why": f"官方 registry 今天查到 {m} 是 {gb}GB"})
            return out

    # 一个都没查成：回退本地清单并**说清是回退**
    m = cands[0]
    gb, when = cached(m)
    out.update({"picked": m, "gb": gb, "source": "offline",
                "checked": when,
                "why": "没能连上官方 registry（离线或未授权）—— 这是本地清单里的候选，"
                       "体积以实际下载为准"})
    return out


def pull_via_ollama(model: str, on_progress=None,
                    base: str = "http://127.0.0.1:11434") -> dict:
    """让**本机的 ollama** 去拉模型（它自己下载，我们只读进度）。

    走回环地址，`guard` 默认放行；不碰模型文件本身。
    `on_progress(status, completed, total)` 每次回调一次，用来更新界面。
    """
    body = json.dumps({"model": model, "stream": True}).encode()
    req = urllib.request.Request(base + "/api/pull", data=body,
                                 headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机不走代理
    last: dict = {}
    try:
        with op.open(req, timeout=3600) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:                           # noqa: BLE001
                    continue
                last = ev
                if on_progress:
                    on_progress(ev.get("status", ""), ev.get("completed") or 0,
                                ev.get("total") or 0)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"ollama 拒绝了这次拉取：HTTP {exc.code}"}
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if last.get("error"):
        return {"ok": False, "error": str(last["error"])}
    return {"ok": True, "status": last.get("status", "success"), "model": model}
