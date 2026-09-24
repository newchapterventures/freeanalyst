#!/usr/bin/env python3
"""本地网页 —— 把 `appraise` 那条命令行搬到浏览器里。

    python3 freeanalyst.py ui ~/deals/某个标的/
    python3 webapp.py ~/deals/某个标的/ --port 8765

## 为什么用标准库起服务（而不是 FastAPI / Flask）

这个项目的底线是**零第三方依赖**（唯一的例外是 PDF 解析的 pdfplumber，
而且不装也能跑）。为了一个本地单用户的界面引一个 Web 框架进来，
会把"能审计"这件事变模糊 —— 多一个依赖就多一份不明代码。
`http.server` 够用：一次一个人用、只在本机、每步都是同步的。

## 只绑 127.0.0.1

**不绑 0.0.0.0。** 绑了的话，同一个 WiFi 下任何人都能打开这个页面、
进而读到你这台机器上的尽调材料。这条不是偏好，是红线 ——
和 `guard.py` 只放行回环是同一类设计。

## 六步向导，这一版做到哪

设计稿见 `docs/interface-mockup.html`（六步：丢材料 / 提取核对 / 行业建议 /
可比公司 / 假设清单 / 结论）。**这一版做到第 1、2、5、6 步**，
中间两步（行业建议、可比公司选取）需要先把流程层接进来，还没做。
页面上会照实说明 —— 不做成"看起来能用"。

## 每一步背后都是一个已经存在的函数

    第 1、2 步   intake.scan()     认材料、判单位口径、跑勾稽
    第 5 步      intake.questions() 把必须由人给的假设列出来
    第 6 步      value.run_report() 出报告（材料目录驱动，同一条报告流）

界面不重写任何判断逻辑。**它只是一层皮** —— 底下是谁算的、怎么算的，
跟命令行那条路完全一样。
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import intake
from intake import Answer, Materials, parse_answer

HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: 接口清单与版本 —— 页面拿它跟自己对表。
#: **真踩过**：页面加了「选择文件夹…」，但跑着的服务还是旧进程（Python 代码不会热加载），
#: 于是点下去只回一句 `unknown endpoint`。现在页面能自己发现这件事并说清楚。
VERSION = "0.40"
ENDPOINTS = ("health", "scan", "appraise", "pick", "config", "gate", "cloud-check",
             "pull", "pull-status", "model-check", "onboarded")
#: 页面依赖的**能力**标记（比接口更细一层：同一个接口也可能少字段）。
#: 页面会逐条核对，缺哪条就提示"服务是旧进程"。
#: 真踩过：百分比字段加进引擎后没重启服务，页面上那些框**静默地没有 %** ——
#: 用户于是不知道填 5、0.05 还是 5%。
FEATURES = ("percent-unit", "llm-config", "install-model", "onboard-state")

#: 项目根目录 —— 页面正文和默认输出都相对它。
ROOT = Path(__file__).resolve().parent

#: 页面正文放在单独文件里 —— **线上那一页和设计稿是同一个文件**。
#: 好处不只是省事：不存在「稿子改好看了、实现没跟上」这种分岔。
#: `webapp_page.html` 可以直接双击打开看样式（没有后端时它自己进设计预览模式）。
PAGE_PATH = ROOT / "webapp_page.html"
#: 说明文件（功能 / 免责声明 / 模型 / 使用 / 调试 / 接自己的大模型）—— 独立成页，
#: 因为它给的是**使用者**，而 README 给的是开发者/审计者，两边读者不同。
DOC_PATH = ROOT / "webapp_doc.html"
#: 模型配置页（本机运行时 / 云端服务商 / 用途绑定 / 出网审计）。
CONFIG_HTML_PATH = ROOT / "webapp_config.html"
#: 除接口之外还提供哪些页面 —— 页面据此判断「服务是不是旧进程」
PAGES = ("doc", "config")


def doc_html() -> str:
    """说明文件页。缺失时给一句人话，不抛 500。"""
    if DOC_PATH.exists():
        return DOC_PATH.read_text(encoding="utf-8")
    return ("<!DOCTYPE html><meta charset='utf-8'>"
            "<body style='font:14px monospace;background:#05070A;color:#D7F5E9;padding:40px'>"
            "说明文件缺失：webapp_doc.html 不在仓库里。</body>")


def config_html() -> str:
    """模型配置页。缺失时给一句人话，不抛 500。"""
    if CONFIG_HTML_PATH.exists():
        return CONFIG_HTML_PATH.read_text(encoding="utf-8")
    return ("<!DOCTYPE html><meta charset='utf-8'>"
            "<body style='font:14px monospace;background:#05070A;color:#D7F5E9;padding:40px'>"
            "配置页缺失：webapp_config.html 不在仓库里。</body>")


def page_html() -> str:
    """要发出去的页面。**优先读 `webapp_page.html`**，没有就退回内置那份。

    在页面上改动频繁的时候，单独一个 html 文件比在 Python 字符串里改要好得多：
    能直接双击看、能用编辑器的语法高亮、diff 也看得清。
    """
    if PAGE_PATH.exists():
        return PAGE_PATH.read_text(encoding="utf-8")
    return PAGE

#: 这次会话扫过的材料（`path → Materials`）。**扫一次就够** ——
#: PDF 走一遍 OCR 可能要几分钟，点一下重扫一遍没人受得了。
_CACHE: dict[str, Materials] = {}


def pick_path(kind: str = "dir") -> dict:
    """让**操作系统**弹一个原生选择框，把真实路径返回给页面。

    ## 为什么不是浏览器的文件选择框（`<input type=file>`）

    浏览器出于沙箱，`<input type=file>` 只给文件名，**拿不到真实路径**；
    想拿到内容就只能把文件**上传**一份 —— 那等于把机密材料复制到别处去，
    对一个「材料不出本机」的工具是不能接受的。

    而我们的服务本来就跑在这台机器上，所以让它去调系统的选择框：
    用户选完，直接拿到真实路径，**一个字节都不复制**，材料还在原地。

    只做 macOS（`osascript`）。其他系统返回一句人话，让人手工填路径 ——
    不做「看起来能用」的假按钮。
    """
    if sys.platform != "darwin":
        return {"ok": False,
                "error": f"原生选择框只做了 macOS（当前系统 {sys.platform}）—— "
                         "请直接填路径，或把文件夹从访达拖进页面"}
    prompt = "选择尽调材料目录" if kind == "dir" else "选择材料文件"
    script = (f'POSIX path of (choose folder with prompt "{prompt}")' if kind == "dir"
              else f'POSIX path of (choose file with prompt "{prompt}")')
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True,
                             text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "选择框等待超时"}
    except OSError as exc:
        return {"ok": False, "error": f"调不起系统选择框：{exc}"}
    if out.returncode != 0:
        # 用户按了取消。**取消不是错误** —— 界面上不该弹红字。
        return {"ok": False, "cancelled": True}
    path = out.stdout.strip().rstrip("/") or "/"
    return {"ok": True, "path": path}


# ─────────────────────── 接口层：把现有函数包成 JSON ───────────────────────

def api_scan(path: str, unit: str = "") -> dict:
    """第 1、2 步：认材料 + 提取核对。"""
    mat = intake.scan(path, unit=unit)
    _CACHE[str(path)] = mat
    return material_json(mat)


def material_json(mat: Materials) -> dict:
    tables = []
    for kind in ("balance", "income", "cash_flow"):
        name = mat.detected.get(kind)
        c = next((x for x in mat.candidates
                  if x.kind == kind and name and x.path.name == name), None)
        tables.append({
            "kind": kind,
            "label": intake.LABEL[kind],
            "file": name or "",
            "mapped": c.mapped if c else 0,
            "rows": c.rows if c else 0,
            "rate": round(c.rate * 100, 1) if c else 0.0,
            "found": c is not None,
        })

    checks = []
    if mat.statements is not None:
        for c in mat.statements.checks():
            checks.append({
                "name": getattr(c, "name", "") or getattr(c, "title", "") or "勾稽",
                "ok": c.ok,
                "applicable": bool(c.applicable),
            })

    s = mat.statements
    return {
        "ok": mat.statements is not None,
        "path": str(mat.directory),
        "label": mat.label,
        "is_file": mat.is_file,
        "source": mat.source,
        "tables": tables,
        "unit": mat.unit,
        "unit_basis": mat.unit_basis,
        "gaap": getattr(s, "gaap", "") if s else "",
        "scope": getattr(s, "scope", "") if s else "",
        "audited": getattr(s, "audited", "") if s else "",
        "period": getattr(s, "period", "") if s else "",
        "checks": checks,
        "unused": mat.unused,
        "notes": mat.notes,
        "warnings": list(getattr(s, "warnings", []) or []) if s else [],
        # 映射率过低 → 界面上要顶一条红字（"表没读懂" ≠ "报表里没有"）
        "mapping_warnings": (s.mapping_warnings() if s is not None else []),
        "questions": [q.__dict__ for q in intake.questions(mat)],
        "out_dir": str(intake.out_dir_for(mat)),
    }


def api_appraise(path: str, unit: str, answers: dict[str, str],
                 out_dir: str | None = None) -> dict:
    """第 5、6 步：把表单里的回答变成配置，出报告。"""
    key = str(path)
    mat = _CACHE.get(key) or intake.scan(path, unit=unit)
    _CACHE[key] = mat

    ans: dict[str, Answer] = {}
    for k, v in (answers or {}).items():
        v = (v or "").strip()
        if v:
            ans[k] = parse_answer(v)
    if unit and not ans.get("unit"):
        ans["unit"] = parse_answer(unit)

    # 百分比字段：界面上把 % 显示在框里，人只填数值（填 8.5 即 8.5%）。
    # **在引擎拿到之前把 % 补进字符串** —— 值自带单位，下游就不用猜；
    # 猜错的代价是 100 倍级的静默错误（8.5 vs 850%）。只走网页这条路。
    intake.normalize_percents(mat, ans)

    if not (ans.get("unit") or mat.unit):
        return {"ok": False, "error": "单位还没确定（报表可能是千元/千美元，"
                                      "引擎默认万元 —— 差 1000 倍）。"
                                      "在上面的「金额单位」里填一个。"}

    # 口径三项：页面上能覆盖材料推出来的值（机器只推，改由人定）
    for attr in ("gaap", "scope", "audited"):
        if ans.get(attr):
            setattr(mat.statements, attr, ans[attr].value.strip())

    cfg, missing = intake.build_config(mat, ans)
    from value import run_report

    out = intake.out_dir_for(mat, out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = intake._slug(cfg.get("target") or "估值")
    cfg_path = out / f"{stem}.config.json"
    report_path = out / f"{stem}.报告.txt"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    report = run_report(cfg, cfg_path.parent, statements=mat.statements)
    report_path.write_text(report, encoding="utf-8")
    note_appraise()          # 记一笔"用过"—— 引导据此不再打扰（见 usage_state）

    return {
        "ok": True,
        "report": report,
        "missing": list(dict.fromkeys(missing)),
        "files": {"config": str(cfg_path), "report": str(report_path)},
        "out_dir": str(out),
    }


# ─────────────────── 模型配置页（本机 / 云端 / 用途 / 审计） ───────────────────

def api_audit(n: int = 20) -> list[dict]:
    """出网审计的尾巴 —— **让"发了什么"看得见**。

    审计里记的是 主机 / 用途 / 字节数 / sha256 / 谁授权 / 发了什么的描述，
    **不记内容本身**（否则审计文件自己会变成第二个泄密点）。
    """
    import guard
    p = Path(guard.AUDIT_PATH)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").strip().splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def api_config() -> dict:
    """配置页要的全部数据。**密钥只给打码值。**"""
    from llm import backends as lb
    from llm import cloud
    from llm import config as lcfg

    cfg = lcfg.load()
    runtimes: list[dict] = []

    ob = lb.OllamaBackend(base_url=cfg["local"]["ollama_url"])
    ok, why = ob.available()
    models: list[str] = []
    if ok:
        try:
            models = ob.list_models()
        except Exception as exc:                        # noqa: BLE001
            why = f"{why}；列模型失败：{exc}"
    runtimes.append({"name": "ollama", "label": "Ollama",
                     "url": cfg["local"]["ollama_url"], "ok": ok, "why": why,
                     "models": models})

    if cfg["local"].get("openai_compat_enabled"):
        cb = lb.OpenAICompatBackend(base_url=cfg["local"]["openai_compat_url"])
        ok2, why2 = cb.available()
        ms: list[str] = []
        if ok2:
            try:
                ms = cb.list_models()
            except Exception as exc:                    # noqa: BLE001
                why2 = f"{why2}；列模型失败：{exc}"
        runtimes.append({"name": "openai-compat", "label": "OpenAI 兼容（LM Studio / vLLM / llama.cpp）",
                         "url": cfg["local"]["openai_compat_url"], "ok": ok2,
                         "why": why2, "models": ms})

    providers = []
    for name, info in cloud.PROVIDERS.items():
        c = cfg["cloud"].get(name, {})
        key = lcfg.key_for(cfg, name)
        providers.append({
            "name": name, "label": info["label"], "kind": info["kind"],
            "host": info["host"], "models": list(info["models"]),
            "note": info.get("note", ""),
            "enabled": bool(c.get("enabled")),
            "key_display": cloud.mask(key), "has_key": bool(key),
            "key_file": (c.get("api_key_file") or info.get("key_file") or ""),
        })

    return {"ok": True, "runtimes": runtimes, "providers": providers,
            "purposes": cfg["purposes"], "purpose_label": lcfg.PURPOSE_LABEL,
            "local": cfg["local"],
            "config_path": str(lcfg.DEFAULT_PATH), "audit": api_audit(8),
            # 指引（哪台机器配哪个模型）—— **单一来源在 llm/guide.py**，
            # 页面只渲染，不自己写一份建议：两处各写一套，早晚对不上。
            "guide": guide_payload()}


def guide_payload() -> dict:
    """给页面/说明文件用的指引数据：分档建议 + 本机实测 + 怎么选 + 没有模型怎么办。"""
    from llm import guide

    d = guide.load_measured()
    ram = guide.detect_ram_gb() or (d.get("machine") or {}).get("ram_gb") or 0
    return {"tiers": [dict(t) for t in guide.NO_GPU_TIERS],
            "measured": guide.measured_rows(),
            "machine": (d.get("machine") or {}),
            "howto": list(guide.HOWTO),
            "speed_tiers": [{"min": a, "name": b, "note": c}
                            for a, b, c in guide.SPEED_TIERS],
            # 还没有本地模型的人：三条路 + 照做三步 + 按内存推荐的 pull 命令
            "ram_gb": ram,
            "recommend": guide.recommend(ram),
            "no_model_paths": [dict(p) for p in guide.NO_MODEL_PATHS],
            "install_steps": list(guide.INSTALL_STEPS),
            # 质量门槛实测（本地 vs 云端）—— **指引里最该说清的一句话**
            "quality": guide.quality_rows(),
            "truth": guide.truth_lines()}


def api_config_save(body: dict) -> dict:
    """只认这三种字段（本机地址 / 云端开关与密钥 / 用途绑定），别的一律忽略。"""
    from llm import cloud
    from llm import config as lcfg

    cfg = lcfg.load()
    local = body.get("local") or {}
    for k in ("ollama_url", "openai_compat_url"):
        if local.get(k):
            cfg["local"][k] = str(local[k]).strip()
    if "openai_compat_enabled" in local:
        cfg["local"]["openai_compat_enabled"] = bool(local["openai_compat_enabled"])

    for name, c in (body.get("cloud") or {}).items():
        if name not in cloud.PROVIDERS:
            continue
        tgt = cfg["cloud"].setdefault(name, {"enabled": False, "api_key": "",
                                             "api_key_file": ""})
        if "enabled" in c:
            tgt["enabled"] = bool(c["enabled"])
        if c.get("api_key_file") is not None:
            tgt["api_key_file"] = str(c.get("api_key_file") or "").strip()
        # 密钥：空字符串 = **不改**（避免"界面拿不到原文 → 一保存就把密钥清空"）
        if str(c.get("api_key") or "").strip():
            tgt["api_key"] = str(c["api_key"]).strip()

    for purpose, model in (body.get("purposes") or {}).items():
        if purpose in lcfg.PURPOSE_LABEL:
            cfg["purposes"][purpose] = str(model).strip()

    p = lcfg.save(cfg)
    return {"ok": True, "path": str(p), "config": lcfg.masked(lcfg.load())}


def api_gate(model: str, runs: int = 1) -> dict:
    """跑一次模型质量门槛。**这是"能用不能用"的裁决，不是跑分。**"""
    from llm import gate as lg

    if not model.strip():
        return {"ok": False, "error": "先选一个模型"}
    try:
        r = lg.run_gate(model.strip(), timeout=900)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "model": r.model, "passed": r.passed, "score": r.score,
            "failed": list(r.failed), "error": r.error, "report": r.report,
            "line": r.line(), "detail": lg.detail_lines(r)}


def api_cloud_check(provider: str, model: str, what: str) -> dict:
    """云端**试一次** —— 这一次调用要按次授权，授权也写进审计。

    发出去的只有一句合成测试话术（**不含任何材料内容**），
    `what` 是人在界面上确认过的"这次到底发什么"。
    """
    from guard import CloudConsent
    from llm import cloud
    from llm import config as lcfg

    info = cloud.PROVIDERS.get(provider)
    if not info:
        return {"ok": False, "error": f"不认识的服务商 {provider}"}
    if info["kind"] != "ready":
        return {"ok": False, "error": f"{info['label']} 需要单独适配（{info.get('note','')}）"}

    cfg = lcfg.load()
    if not (cfg["cloud"].get(provider, {}) or {}).get("enabled"):
        return {"ok": False, "error": f"{info['label']} 还没在配置里启用 —— "
                                      "启用之后才能试（云端默认关闭是刻意的）"}
    b = cloud.build(provider, api_key=lcfg.key_for(cfg, provider))
    ok, why = b.available()
    if not ok:
        return {"ok": False, "error": why}

    prompt = "请只回复两个字：可用"
    consent = CloudConsent(
        host=b.host, purpose=f"配置页试一次：{info['label']}",
        what=(what.strip() or "一句话测试（不含任何材料内容）"),
        approved_by="user:配置页确认")
    try:
        r = b.generate(model or b.models[0], prompt=prompt, system="",
                       timeout=60, consent=consent)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "reply": (r.text or "").strip()[:80],
            "host": b.host, "model": r.model,
            "sent_bytes": len(prompt.encode("utf-8"))}


# ─────────────────── 使用状态：引导该不该出现 ───────────────────
#
# **为什么记在服务端而不是浏览器**：浏览器把"看过"记在 localStorage 里是按**站点**
# 记的，而这个服务端口会变（8765 被占就往后换一个）—— 换了端口就是一个新站点，
# 引导会**再弹一次**；反过来清了缓存又会被拦一次教学。
# 所以"这台机器上用过没有"记在本地文件里，按安装记，不按浏览器记。

#: 使用状态文件。**可以用环境变量改路径** —— 测试必须能把它指到临时目录去，
#: 否则跑一次测试就把人真实的使用记录改了（真踩过：测试跑了 10 次估值，
#: 用户的 runs 直接变成 10）。
UI_STATE = Path.home() / ".freeanalyst" / "ui-state.json"


def _running_tests() -> bool:
    """在跑 unittest 吗 —— 跑测试时**不许写用户真实的使用记录**。

    为什么要有这个判断：测试里有好几处会真的跑 `api_appraise`，而它会记一笔
    "用过"。真踩过：跑一次测试，用户的 runs 从 0 变成 10，引导从此再也不弹。
    靠测试自己去设环境变量不可靠（`discover -s tests` 不会导入包级 `__init__`），
    所以这里主动让开。
    """
    import sys

    return "unittest" in sys.modules and "unittest" in " ".join(sys.argv[:2])


def _ui_state_path() -> Path:
    import os

    env = os.environ.get("FREANALYST_UI_STATE")
    if env:
        return Path(env)
    if _running_tests():
        global _TEST_STATE
        if _TEST_STATE is None:
            import tempfile

            _TEST_STATE = (Path(tempfile.mkdtemp(prefix="freeanalyst-test-"))
                           / "ui-state.json")
        return _TEST_STATE
    return UI_STATE


_TEST_STATE: Path | None = None


def _load_ui_state() -> dict:
    try:
        return json.loads(_ui_state_path().read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def _save_ui_state(d: dict) -> None:
    p = _ui_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:                                       # noqa: BLE001
        pass                                                # 记不住不是错误，别打断人


def usage_state() -> dict:
    """这台机器上用过没有 —— 页面据此决定要不要弹引导。"""
    d = _load_ui_state()
    return {"onboarded": bool(d.get("onboarded_at")),
            "runs": int(d.get("runs") or 0),
            "first_run": d.get("first_run") or ""}


def note_appraise() -> None:
    """成功出了一次估值 —— **这是"用过"的最强信号**（比"看过引导"强）。"""
    d = _load_ui_state()
    d["runs"] = int(d.get("runs") or 0) + 1
    d.setdefault("first_run", time.strftime("%Y-%m-%d %H:%M"))
    _save_ui_state(d)


def api_onboarded() -> dict:
    """页面点完引导后回记一笔（服务端 + 浏览器各记一次，互为兜底）。"""
    d = _load_ui_state()
    d.setdefault("onboarded_at", time.strftime("%Y-%m-%d %H:%M"))
    _save_ui_state(d)
    return {"ok": True, **usage_state()}


# ─────────────────── 装模型：服务端拉取，不用开终端 ───────────────────
#
# 为什么放在服务端做：让**本机的 ollama**自己去下载，界面只读进度。
# 用户点一下按钮就行 —— 不用开终端、不用记 `ollama pull` 怎么写。
# 拉的是回环地址（127.0.0.1:11434），不是外部主机。

_PULLS: dict[str, dict] = {}
_PULL_LOCK = threading.Lock()


def api_pull_start(model: str) -> dict:
    """开始拉一个模型（后台线程），立刻返回 —— 界面轮询进度。"""
    from llm import registry

    model = (model or "").strip()
    if not model:
        return {"ok": False, "error": "先填一个模型名，例如 qwen3.5:9b"}
    # **按形状校验，不是只列字符白名单。** 只列白名单时会漏掉 `../../etc/passwd`
    # 这种"字符合法但形状荒唐"的名字（实测被测试抓出来过）。
    import re as _re

    if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(:[A-Za-z0-9._-]+)?", model) \
            or ".." in model or len(model) > 80:
        return {"ok": False,
                "error": "模型名形状不对 —— 应该是 `家族:档位`（如 qwen3.5:9b）："
                         "以字母数字开头，只含字母数字与 . _ - ，不含 `..`"}

    with _PULL_LOCK:
        cur = _PULLS.get("current") or {}
        if cur.get("running"):
            return {"ok": False, "error": f"已经在拉 {cur.get('model')}，等它跑完"}
        _PULLS["current"] = {"model": model, "running": True, "status": "开始…",
                             "completed": 0, "total": 0, "error": "", "ok": None}

    def _run() -> None:
        def on_progress(status: str, done: int, total: int) -> None:
            with _PULL_LOCK:
                _PULLS["current"].update({"status": status, "completed": done,
                                          "total": total})

        r = registry.pull_via_ollama(model, on_progress=on_progress)
        with _PULL_LOCK:
            _PULLS["current"].update({"running": False, "ok": bool(r.get("ok")),
                                      "error": r.get("error", ""),
                                      "status": r.get("status", "")})

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "model": model}


def api_pull_status() -> dict:
    with _PULL_LOCK:
        return dict(_PULLS.get("current") or {"model": "", "running": False})


def api_model_check(ram_gb: float = 0, consent_ok: bool = False) -> dict:
    """查"当前该装哪个"。

    `consent_ok` = 用户在界面上确认过这次联网（界面会把 **主机 / 用途 / 这次发什么**
    原样写出来）。没有它就不联网，回退本地清单并**说明是回退**。
    """
    from guard import CloudConsent
    from llm import registry

    consent = None
    if consent_ok:
        consent = CloudConsent(
            host=registry.HOST,
            purpose="查模型体积与当前代次（只发模型名，不含材料）",
            what="模型家族名（如 qwen3.5）—— 不含任何材料内容、公司名、文件名",
            approved_by="user:配置页确认")
    return {"ok": True, **registry.check_candidates(ram_gb, consent=consent,
                                                    refresh=bool(consent_ok))}


# ─────────────────────────── HTTP 层 ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "FreeAnalyst"

    def log_message(self, format, *args):              # noqa: A002 - 与基类同签名
        """别把每次请求都打到终端 —— 这里不是给日志用的服务。"""
        pass

    # ---------- 工具 ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 本地工具，**不许任何外部页面把它嵌进 iframe**（防点击劫持）
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------- 路由 ----------
    def do_GET(self):                                  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, page_html().encode(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._json({"ok": True, "version": VERSION,
                        "endpoints": list(ENDPOINTS), "pages": list(PAGES),
                        "features": list(FEATURES),
                        # 用过没有 —— 页面据此决定要不要弹引导（见 usage_state）
                        "usage": usage_state()})
        elif self.path in ("/doc", "/doc.html"):
            self._send(200, doc_html().encode(), "text/html; charset=utf-8")
        elif self.path in ("/config", "/config.html"):
            self._send(200, config_html().encode(), "text/html; charset=utf-8")
        elif self.path == "/api/config":
            self._json(api_config())
        elif self.path == "/api/audit":
            self._json({"ok": True, "lines": api_audit(20)})
        elif self.path == "/api/pull-status":
            self._json(api_pull_status())
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):                                 # noqa: N802
        body = self._read_json()
        try:
            if self.path == "/api/scan":
                p = (body.get("path") or "").strip()
                if not p:
                    self._json({"ok": False, "error": "先给材料路径"}, 400)
                    return
                if not Path(p).expanduser().exists():
                    self._json({"ok": False, "error": f"这个路径不存在：{p}"}, 400)
                    return
                self._json(api_scan(p, body.get("unit") or ""))
            elif self.path == "/api/pick":
                # 弹系统原生选择框，把真实路径回给页面（不复制任何文件）
                self._json(pick_path(body.get("kind") or "dir"))
            elif self.path == "/api/appraise":
                self._json(api_appraise(body.get("path") or "",
                                        body.get("unit") or "",
                                        body.get("answers") or {}))
            elif self.path == "/api/config":
                self._json(api_config_save(body or {}))
            elif self.path == "/api/gate":
                self._json(api_gate(body.get("model") or "", body.get("runs") or 1))
            elif self.path == "/api/cloud-check":
                self._json(api_cloud_check(body.get("provider") or "",
                                           body.get("model") or "",
                                           body.get("what") or ""))
            elif self.path == "/api/pull":
                self._json(api_pull_start(body.get("model") or ""))
            elif self.path == "/api/model-check":
                self._json(api_model_check(float(body.get("ram_gb") or 0),
                                           bool(body.get("consent_ok"))))
            elif self.path == "/api/onboarded":
                self._json(api_onboarded())
            else:
                self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:                       # noqa: BLE001
            # **不许静默吞掉** —— 界面要看见失败，否则人以为在跑。
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


# ─────────────────────────── 页面 ───────────────────────────
# 六步向导：这一版做到 1、2、5、6（3、4 照实标注未接）。

PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>FreeAnalyst · 本地估值向导</title>
<style>
  :root{ --fg:#101828; --mut:#667085; --line:#e4e7ec; --acc:#4743E8; --bg:#fff; }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif}
  .wrap{max-width:960px;margin:0 auto;padding:34px 26px 80px}
  h1{font-size:21px;margin:0 0 4px;letter-spacing:.01em}
  .sub{color:var(--mut);font-size:12.5px;margin-bottom:22px}
  .steps{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:24px}
  .step{border:1px solid var(--line);border-radius:20px;padding:5px 13px;font-size:12px;
        color:var(--mut);display:flex;gap:7px;align-items:center}
  .step .n{font-variant-numeric:tabular-nums;font-weight:650}
  .step.on{border-color:var(--acc);color:var(--acc);font-weight:600}
  .step.off{opacity:.45;text-decoration:line-through}
  .card{border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px;
        transition:border-color .18s ease}
  .card:hover{border-color:var(--acc)}
  .card h2{font-size:14px;margin:0 0 10px}
  .card h2 span{font-weight:400;color:var(--mut);font-size:12px;margin-left:8px}
  input[type=text]{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
        font-size:13px;font-family:inherit;color:var(--fg);background:transparent}
  input[type=text]:focus{outline:none;border-color:var(--acc)}
  button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:9px 18px;
        font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
  button:disabled{opacity:.45;cursor:default}
  button.ghost{background:transparent;color:var(--acc);border:1px solid var(--acc)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line)}
  th{color:var(--mut);font-weight:600;font-size:11.5px}
  .ok{color:var(--acc);font-weight:600}
  .bad{color:#c0392b;font-weight:600}
  .mut{color:var(--mut)}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .q{display:grid;grid-template-columns:230px 1fr;gap:10px;align-items:start;
     padding:8px 0;border-bottom:1px solid var(--line)}
  .q label{font-size:12.5px}
  .q .hint{display:block;color:var(--mut);font-size:11.5px;margin-top:2px}
  .q input{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  pre{background:#f7f8fa;border:1px solid var(--line);border-radius:10px;padding:14px;
      overflow:auto;max-height:520px;font-size:11.5px;line-height:1.55;white-space:pre-wrap}
  .banner{border:1px dashed var(--line);border-radius:10px;padding:11px 14px;
          color:var(--mut);font-size:12.5px;margin-bottom:16px}
  .banner b{color:var(--fg)}
  .hide{display:none}
</style></head><body><div class="wrap">

  <h1>FreeAnalyst · 本地估值向导</h1>
  <div class="sub">本地跑的一个网页，全程不出网。每一步都是「程序准备好、你点头才往下走」。<br>
    <b>计算一个字都没改</b> —— 底下跑的就是命令行那条路（<code>intake.scan</code> → <code>value.run_report</code>）。</div>

  <div class="steps">
    <div class="step on" id="s1"><span class="n">1</span>丢材料</div>
    <div class="step" id="s2"><span class="n">2</span>提取核对</div>
    <div class="step off" id="s3"><span class="n">3</span>行业建议（未接）</div>
    <div class="step off" id="s4"><span class="n">4</span>可比公司（未接）</div>
    <div class="step" id="s5"><span class="n">5</span>假设清单</div>
    <div class="step" id="s6"><span class="n">6</span>结论</div>
  </div>

  <div class="banner">
    <b>这一版做到第 1、2、5、6 步。</b>第 3、4 步（行业建议、可比公司选取）需要先把流程层接进来，
    还没做 —— 画上去会变成"看起来能用"。可比公司代码可以在第 5 步的「可选」里填，那一条是通的。
  </div>

  <div class="card">
    <h2>1 · 丢材料<span>一个目录，或者单独一份 PDF</span></h2>
    <div class="row">
      <input type="text" id="path" placeholder="/Users/you/deals/某个标的/ 或 某公司2024年审计报告.pdf">
      <div><button id="scanBtn" onclick="scan()">认材料</button></div>
    </div>
    <div id="scanMsg" class="mut" style="margin-top:9px"></div>
  </div>

  <div class="card hide" id="card2">
    <h2>2 · 提取核对<span>三张表认出来没有、勾稽平不平、单位与口径是什么</span></h2>
    <div id="tables"></div>
    <div id="checks" style="margin-top:12px"></div>
    <div id="meta" style="margin-top:12px"></div>
    <div id="unitBox" style="margin-top:12px"></div>
  </div>

  <div class="card hide" id="card5">
    <h2>5 · 假设清单<span>必须由你给的数 —— 引擎不替你决定</span></h2>
    <div class="banner" style="margin:0 0 12px">
      每个数后面可以跟来源：<code>0.10 @管理层规划 p.12</code>；来源前写 <code>高:</code> / <code>中:</code> / <code>低:</code>
      可指定置信度。<b>不写来源 = 低置信度</b>，会出现在报告的"结果的软肋"里。
      注释里的「参考」是历史值，**不是建议值**。
    </div>
    <div id="questions"></div>
    <div style="margin-top:16px"><button id="goBtn" onclick="go()">出报告</button></div>
  </div>

  <div class="card hide" id="card6">
    <h2>6 · 结论<span id="outDir" class="mut"></span></h2>
    <div id="missing" style="margin-bottom:12px"></div>
    <pre id="report"></pre>
  </div>

<script>
const $ = id => document.getElementById(id);
let STATE = { path:"", unit:"", data:null };

function step(id, cls){
  const el = $(id);
  el.className = "step" + (cls ? " " + cls : "");
}

async function post(url, body){
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                              body: JSON.stringify(body)});
  return await r.json();
}

async function scan(){
  const p = $("path").value.trim();
  if(!p){ $("scanMsg").textContent = "先填一个路径"; return; }
  $("scanBtn").disabled = true;
  $("scanMsg").textContent = "在认材料…（PDF 走 OCR 可能要几分钟，别关页面）";
  const d = await post("/api/scan", {path:p, unit:STATE.unit});
  $("scanBtn").disabled = false;
  if(!d.ok){
    $("scanMsg").innerHTML = '<span class="bad">' + (d.error || "没认出来") + '</span>';
    if(d.tables){ renderTables(d); renderMeta(d); }
    return;
  }
  STATE.path = d.path; STATE.data = d;
  $("scanMsg").innerHTML = '<span class="ok">认出来了</span>';
  renderTables(d); renderChecks(d); renderMeta(d); renderUnit(d); renderQuestions(d);
  step("s2","on"); step("s5","on"); step("s6","on");
  $("card2").classList.remove("hide"); $("card5").classList.remove("hide");
  $("card2").scrollIntoView({behavior:"smooth"});
}

function renderTables(d){
  let h = '<table><tr><th>表</th><th>来源</th><th>映射</th><th>状态</th></tr>';
  for(const t of d.tables){
    h += '<tr><td>'+t.label+'</td><td class="mut">'+(t.file||"—")+'</td>'+
         '<td class="mut">'+(t.rows? t.mapped+"/"+t.rows+" 行（"+t.rate+"%）":"—")+'</td>'+
         '<td>'+(t.found?'<span class="ok">认出来了</span>':'<span class="bad">没认出来</span>')+'</td></tr>';
  }
  h += '</table>';
  if(d.unused && d.unused.length){
    h += '<div class="mut" style="margin-top:10px;font-size:12px"><b>没用上的文件（不是静默跳过）</b><br>'+
         d.unused.map(u=>"· "+u.replace(/</g,"&lt;")).join("<br>")+'</div>';
  }
  $("tables").innerHTML = h;
}

function renderChecks(d){
  let h = '<table><tr><th>勾稽校验</th><th>结果</th></tr>';
  for(const c of d.checks){
    let v = c.ok===true ? '<span class="ok">平</span>'
          : c.ok===false ? '<span class="bad">不平 —— 推算结果别用</span>'
          : c.applicable ? '<span class="mut">判不了（缺科目）</span>'
          : '<span class="mut">不适用（这种格式没这条）</span>';
    h += '<tr><td>'+c.name+'</td><td>'+v+'</td></tr>';
  }
  const bad = d.checks.some(c=>c.ok===false);
  if(bad){
    const dups = (d.warnings||[]).filter(w=>w.includes("个取值")).length;
    if(dups) h += '<tr><td colspan="2" class="mut">这份材料有 '+dups+
      ' 个科目出现多个取值 —— 很可能是同一页上既有合并表又有母公司表，取值取串了。'+
      '先确认取的是合并那一列。</td></tr>';
  }
  $("checks").innerHTML = h + '</table>';
}

function renderMeta(d){
  $("meta").innerHTML = '<div class="mut" style="font-size:12.5px">口径：'+
    '<b>'+(d.gaap||"未判定")+'</b> · <b>'+(d.scope||"未判定")+'</b> · <b>'+(d.audited||"未标注")+
    '</b> · 期间 <b>'+(d.period||"未标")+'</b>（不对就在下面改）</div>' +
    (d.notes && d.notes.length ? '<div class="mut" style="margin-top:8px;font-size:12px">'+
      d.notes.map(n=>"· "+n).join("<br>")+'</div>' : '');
}

function renderUnit(d){
  $("unitBox").innerHTML =
    '<div class="row"><div><label style="font-size:12.5px">金额单位（<b>错 1000 倍就是这里错</b>）</label>'+
    '<input type="text" id="unit" value="'+(d.unit||"")+'" placeholder="元 / 千元 / 万元 / 千美元"></div>'+
    '<div class="mut" style="align-self:end;font-size:12px">'+
    (d.unit ? "依据："+(d.unit_basis||"") : "<span class='bad'>没认出来 —— 必须你声明</span>")+
    '</div></div>';
}

function renderQuestions(d){
  let g = "";
  let h = "";
  for(const q of d.questions){
    if(q.group !== g){ g = q.group; h += '<h3 style="font-size:12.5px;margin:16px 0 4px;color:var(--acc)">'+g+'</h3>'; }
    const ref = q.reference ? "　｜ 参考："+q.reference : "";
    h += '<div class="q"><label>'+q.label+'<span class="hint">'+(q.hint||"")+ref+'</span></label>'+
         '<input type="text" data-key="'+q.key+'" value="'+(q.default||"").replace(/"/g,"&quot;")+'"></div>';
  }
  $("questions").innerHTML = h;
}

async function go(){
  $("goBtn").disabled = true;
  const answers = {};
  document.querySelectorAll("#questions input").forEach(i=>{ if(i.value.trim()) answers[i.dataset.key]=i.value.trim(); });
  const u = $("unit") ? $("unit").value.trim() : "";
  const d = await post("/api/appraise", {path:STATE.path, unit:u, answers:answers});
  $("goBtn").disabled = false;
  if(!d.ok){ $("missing").innerHTML = '<span class="bad">'+(d.error||"出错了")+'</span>'; return; }
  $("outDir").textContent = "产物：" + d.out_dir;
  let m = "";
  if(d.missing.length){
    m = '<div class="banner" style="margin:0 0 12px"><b>这几个没给，对应的那一块就没跑</b>（不是没发生）<br>'+
        d.missing.map(x=>"· "+x).join("<br>")+'</div>';
  }
  $("missing").innerHTML = m;
  $("report").textContent = d.report;
  $("card6").classList.remove("hide");
  $("card6").scrollIntoView({behavior:"smooth"});
}
</script>
</div></body></html>
"""


def find_port(host: str, want: int, tries: int = 12) -> int:
    """端口被占就往后找一个。

    ## 为什么非要这个（实测踩到）

    第一次交付时，用户在浏览器里看到的是 `ERR_CONNECTION_REFUSED`：
    服务没起来，而页面上没有一行字告诉他为什么。
    **"打不开"绝不能是这种工具给人的第一印象** —— 它得自己说清楚是
    端口被占了、还是别的。

    所以：先试想要的那个，占了就往后挪，并把真实端口**打在屏幕上**、
    也用真实端口去开浏览器。宁可换端口，也不要一句 error。
    """
    for p in range(want, want + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise SystemExit(f"× {want} 起往后 {tries} 个端口都被占了 —— 用 --port 换一个")


def _ready_check(url: str) -> None:
    """起来之后自己请求一遍，成功就在屏幕上打一行。

    这一行的意思是「端口确实通了」—— 不用人靠浏览器去猜。
    失败也照实打出来（**不许静默**）。
    """
    try:
        with urllib.request.urlopen(url + "api/health", timeout=5) as r:
            ok = b'"ok": true' in r.read() or r.status == 200
        print(f"  ✓ 自检通过：{url} 能打开（这一行说明端口通了）" if ok
              else "  × 自检异常：服务起来了但健康检查没通过")
    except Exception as exc:                            # noqa: BLE001
        print(f"  × 自检失败：{type(exc).__name__}: {exc}")
        print("    如果你浏览器里看到「拒绝连接」，多半是**代理**把 127.0.0.1 也劫走了 ——")
        print("    在 Clash 的 bypass/直连列表里加上 127.0.0.1,localhost，或先关掉系统代理。")


def serve(materials: str | None = None, *, port: int = DEFAULT_PORT,
          open_browser: bool = True) -> int:
    """起本地服务。**只绑 127.0.0.1。**

    传了 `materials` 会预扫一遍并把它填进页面（省得手打路径）。
    """
    if materials:
        path = str(Path(materials).expanduser().resolve())
        print(f"预扫材料：{path}")
        try:
            _CACHE[path] = intake.scan(path)
        except Exception as exc:                        # noqa: BLE001
            print(f"  预扫失败（页面上可以重填）：{type(exc).__name__}: {exc}")

    real = find_port(HOST, port)
    if real != port:
        print(f"注意：{port} 被别的程序占了，改用 {real}")
    srv = ThreadingHTTPServer((HOST, real), Handler)
    url = f"http://{HOST}:{real}/"
    print("─" * 66)
    print(f"FreeAnalyst 本地向导　{url}")
    print(f"  只绑 {HOST} —— 同一个 WiFi 下的其他机器**访问不到**")
    print("  按 Ctrl-C 停止")
    print("─" * 66)
    threading.Timer(0.6, lambda: _ready_check(url)).start()
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停了。")
    finally:
        srv.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FreeAnalyst 本地网页向导")
    ap.add_argument("materials", nargs="?", default=None, help="材料目录或单个文件")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true", help="不自动开浏览器")
    args = ap.parse_args()
    return serve(args.materials, port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    sys.exit(main())
