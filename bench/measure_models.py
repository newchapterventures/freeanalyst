"""量一量：这台机器上，每个本机模型**占多少内存、跑多快**。

## 为什么要有这个

"多大内存能跑哪个模型"这句话，如果只是照着模型文件大小说，是骗人的：
文件 9.3GB 的模型，实际驻留内存不止 9.3GB，而且**能跑**和**能忍受**是两件事。
大多数投资人的机器是**没有独立显卡的商务本**，所以这份数据要回答三个问题：

    能不能装下    → 驻留内存（GB），问 ollama 要，不靠估
    多久出第一个字 → 装载耗时（秒）
    写着累不累    → 生成速度（字/秒），从 ollama 自报的 eval_duration 算

## 怎么量（都是服务自报的数，不是掐表猜的）

1. `POST /api/generate` 先空跑一次（把模型装载进内存），再跑一次正式量；
2. 正式那次的 `eval_count / eval_duration` = **生成速度**（tok/s）；
3. 紧接着 `GET /api/ps` 读**驻留内存**（ollama 报的 size，含 KV cache）；
4. `keep_alive` 设短，量完就卸载 —— 免得几个模型叠在一起把机器拖垮。

用量：`python3 bench/measure_models.py --json bench/measured-local.json`
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OLLAMA = "http://127.0.0.1:11434"
#: 让模型认真写点东西的量 —— 太短了测不出速度（首字延迟占主导）
PROMPT = ("请用三句话说明：为什么估值时要把折旧摊销加回营业利润？"
          "只讲道理，不要罗列公式。")
NUM_PREDICT = 160


def _post(url: str, body: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机不走代理
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str, timeout: int = 60) -> dict:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def machine() -> dict:
    """这台机器是什么 —— 指引要写"哪一档机器"，得说清基准。"""
    info: dict = {"os": platform.platform()}
    if platform.system() == "Darwin":
        try:
            info["chip"] = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                          capture_output=True, text=True).stdout.strip()
            mem = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True).stdout.strip()
            if mem:
                info["ram_gb"] = round(int(mem) / 1024 ** 3, 1)
            # 有没有独立显卡（Mac 上问显示芯片）
            sp = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                capture_output=True, text=True).stdout
            info["gpu"] = next((ln.split(":")[1].strip() for ln in sp.splitlines()
                                if "Chipset Model" in ln), "")
        except Exception:                                   # noqa: BLE001
            pass
    return info


def list_models() -> list[dict]:
    d = _get(f"{OLLAMA}/api/tags")
    out = []
    for m in d.get("models", []):
        out.append({"name": m.get("name", ""),
                    # 十进制 GB —— 与 `ollama list` 显示的口径一致
                    # （用 1024³ 会算出 6.1 而 ollama 显示 6.6，用户会以为出错了）
                    "file_gb": round((m.get("size") or 0) / 1000 ** 3, 1),
                    "params": (m.get("details") or {}).get("parameter_size", ""),
                    "quant": (m.get("details") or {}).get("quantization_level", "")})
    return sorted(out, key=lambda x: x["file_gb"])


def resident_gb(name: str) -> float:
    """模型现在占多少内存（ollama 自报，含 KV cache）。十进制 GB，与 ollama 一致。"""
    try:
        for m in _get(f"{OLLAMA}/api/ps").get("models", []):
            if m.get("name") == name or m.get("model") == name:
                return round((m.get("size") or 0) / 1000 ** 3, 1)
    except Exception:                                       # noqa: BLE001
        pass
    return 0.0


def measure(name: str, keep_alive: str = "30s") -> dict:
    """量一个模型：装载耗时 / 生成速度 / 驻留内存。"""
    row: dict = {"name": name}

    t0 = time.time()
    try:
        # 第一次：只为把它装载进来（装载耗时单算，不然首字延迟被冤枉）
        _post(f"{OLLAMA}/api/generate",
              {"model": name, "prompt": "你好", "stream": False,
               "options": {"num_predict": 1}, "keep_alive": keep_alive})
    except Exception as exc:                                # noqa: BLE001
        row["error"] = f"装载失败：{type(exc).__name__}: {exc}"
        return row
    row["load_s"] = round(time.time() - t0, 1)

    try:
        r = _post(f"{OLLAMA}/api/generate",
                  {"model": name, "prompt": PROMPT, "stream": False,
                   "options": {"num_predict": NUM_PREDICT, "temperature": 0.2},
                   "keep_alive": keep_alive})
    except Exception as exc:                                # noqa: BLE001
        row["error"] = f"生成失败：{type(exc).__name__}: {exc}"
        return row

    ev, evd = r.get("eval_count") or 0, r.get("eval_duration") or 0
    row["out_tokens"] = ev
    row["secs"] = round(evd / 1e9, 1)
    row["tok_s"] = round(ev / (evd / 1e9), 1) if evd else 0.0
    # 首字延迟：从提问到开始吐字（提示处理 + 排队），不含装载
    ped = r.get("prompt_eval_duration") or 0
    row["first_token_s"] = round((ped + (r.get("load_duration") or 0)) / 1e9, 1)
    row["resident_gb"] = resident_gb(name)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="量本机模型的驻留内存与速度")
    ap.add_argument("--models", default="", help="逗号分隔；默认量全部本机模型")
    ap.add_argument("--json", default="", help="结果写到这里")
    ap.add_argument("--merge", action="store_true",
                    help="与已有结果**合并**（按模型名覆盖同名条目，其余保留）—— "
                         "新装一个模型时不必把整机重测一遍")
    args = ap.parse_args()

    info = machine()
    print("=" * 74)
    print("这台机器：" + json.dumps(info, ensure_ascii=False))
    print("=" * 74)

    known = {m["name"]: m for m in list_models()}
    names = ([s.strip() for s in args.models.split(",") if s.strip()]
             or list(known))
    rows = []
    for n in names:
        f = known.get(n, {})
        print(f"\n[{n}] 文件 {f.get('file_gb','?')}GB · {f.get('params','?')} "
              f"· {f.get('quant','?')}")
        row = measure(n)
        row.update({"file_gb": f.get("file_gb", 0), "params": f.get("params", ""),
                    "quant": f.get("quant", "")})
        rows.append(row)
        if row.get("error"):
            print("   ✗ " + row["error"])
        else:
            print(f"   装载 {row['load_s']}s · 首字 {row['first_token_s']}s · "
                  f"{row['tok_s']} tok/s（{row['out_tokens']} 字用 {row['secs']}s）· "
                  f"驻留 {row['resident_gb']}GB")

    print("\n" + "=" * 74)
    print(f"{'模型':<26}{'文件':>6}{'驻留':>7}{'装载':>7}{'首字':>7}{'速度':>9}")
    print("-" * 74)
    for r in rows:
        if r.get("error"):
            print(f"{r['name']:<26}  ✗ {r['error'][:40]}")
            continue
        print(f"{r['name']:<26}{r.get('file_gb',0):>5}G{r.get('resident_gb',0):>6}G"
              f"{r.get('load_s',0):>6}s{r.get('first_token_s',0):>6}s"
              f"{r.get('tok_s',0):>8} tok/s")

    if args.json:
        out = {"machine": info, "models": rows}
        if args.merge:
            p = Path(args.json)
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
            except Exception:                               # noqa: BLE001
                old = {}
            by_name = {m.get("name"): m for m in (old.get("models") or [])}
            for r in rows:                                  # 新的覆盖同名的
                by_name[r["name"]] = r
            out["models"] = sorted(by_name.values(),
                                   key=lambda m: m.get("file_gb") or 0)
            print(f"　（合并：原有 {len(old.get('models') or [])} 条，"
                  f"现共 {len(out['models'])} 条）")
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
