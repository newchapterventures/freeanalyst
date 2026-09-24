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
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import intake
from intake import Answer, Materials, parse_answer

HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: 这次会话扫过的材料（`path → Materials`）。**扫一次就够** ——
#: PDF 走一遍 OCR 可能要几分钟，点一下重扫一遍没人受得了。
_CACHE: dict[str, Materials] = {}


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

    return {
        "ok": True,
        "report": report,
        "missing": list(dict.fromkeys(missing)),
        "files": {"config": str(cfg_path), "report": str(report_path)},
        "out_dir": str(out),
    }


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
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._json({"ok": True})
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
            elif self.path == "/api/appraise":
                self._json(api_appraise(body.get("path") or "",
                                        body.get("unit") or "",
                                        body.get("answers") or {}))
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
