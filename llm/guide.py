"""机器配得上哪个模型 —— **怎么写，才不让人白折腾**。

## 这份指引要回答的问题

    我这台笔记本（**多数没有独立显卡**）能跑哪个开源模型？
    跑起来是什么体验 —— 会不会点一下等三分钟？

## 三条不许含糊的事

1. **"能装下"和"能忍"是两件事。** 文件 9GB ≠ 占 9GB 内存，也 ≠ 用着顺手。
   所以分档给两列：能装下（内存）＋ 写起来累不累（速度）。
2. **速度不靠估，靠量。** `bench/measure_models.py` 在**本机**跑一遍，
   驻留内存问 ollama 要、速度用 ollama 自报的 `eval_duration` 算。
   这份实测表放在 `bench/measured-local.json`，页面直接读它 —— **不替它编数**。
3. **没有模型也能用这个工具。** 报表识别、勾稽校验、估值计算**全是代码**，
   模型只用在"问答"那一步。跑不动模型 ≠ 跑不了估值 —— 这句话要写在最前面，
   否则用商务本的人会以为"我这机器用不了"。

## 分档的依据

模型文件大小是**事实**（见 `ollama list`）；"驻留比文件大 1.1~1.3 倍"是经验规则
（权重的显存/内存副本 + KV cache + 运行时开销），所以档位表标的是**推荐**，
不是保证 —— 保证只在 `bench/measured-local.json` 那张实测表里。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: 本机实测（由 `bench/measure_models.py` 生成）。**没有就说没有，不编一个。**
MEASURED = ROOT / "bench" / "measured-local.json"

#: 没有独立显卡的机器（绝大多数商务本 / MacBook Air）
NO_GPU_TIERS: tuple[dict, ...] = (
    {"ram": "8 GB", "rec": "3B~4B（Q4 量化）", "file": "2~3 GB",
     "examples": "qwen3:4b · llama3.2:3b · gemma3:4b",
     "expect": "跑得动；**问答质量别期待太高**（同档 8B 实测 2/5，见下）",
     "verdict": "将就"},
    {"ram": "16 GB", "rec": "7B~8B（Q4）", "file": "4~5 GB",
     "examples": "qwen2.5:7b · qwen3:8b · hermes3:8b",
     "expect": "速度最稳的一档；但**质量门槛实测 2/5**，只能当辅助（答案要人工核对）",
     "verdict": "跑得动"},
    {"ram": "24~32 GB", "rec": "12B~14B（Q4）", "file": "8~10 GB",
     "examples": "qwen3:14b · gemma3:12b",
     "expect": "本机最好的一档：**实测 4/5**（差一项，仍是未过门槛）；速度取决于内存带宽",
     "verdict": "本机最优"},
    {"ram": "32 GB+ 或有独显", "rec": "30B 级 MoE（Q4）", "file": "18~20 GB",
     "examples": "qwen3:30b-a3b",
     "expect": "**实测反而更差（3/5）** —— 本机推理下大模型不等于稳；先过门槛再说",
     "verdict": "别急着上"},
)

#: 速度档位的经验阈值（tok/s）。给的是**感受**，不是跑分：
#: 人读中文大约 5~8 字/秒，所以低于这个速度的模型，你会盯着它一个字一个字往外蹦。
SPEED_TIERS: tuple[tuple[float, str, str], ...] = (
    (25.0, "顺畅", "问一句答一句，几乎不用等"),
    (12.0, "可用", "读得比它写得快一点，可以接受"),
    (5.0, "能忍", "长答案要等十几秒，建议把问题问小一点"),
    (0.0, "别用", "等不起 —— 换更小的模型，或走云端（按次授权）"),
)

#: 推荐档位 → 该跑哪个模型由**本机实际装了什么**决定，不硬写模型名。
HOWTO: tuple[str, ...] = (
    "先跑质量门槛（配置页里的按钮，或 `python3 bench/model_quality.py --models 模型名`）："
    "**先过门槛，再看速度** —— 写得快但答得错的模型，在这个工具里只会添乱。",
    "估值本身不吃模型：报表识别、勾稽校验、DCF／乘数计算全是代码。"
    "没 GPU 的商务本也能完整跑出估值报告，模型只影响「问答」那一步。",
    "材料不用整篇喂给模型 —— 工具是**按问题检索片段**再问的，所以 7B~8B 也能应付。",
    "内存不够就别硬上大模型：系统开始用交换分区时，整台机器都会卡，"
    "而不只是这个工具慢。宁可换小一档。",
    "实在跑不动本机模型：可以走云端（**按次授权**，每次调用都要你点头，"
    "授权进审计）。代价是那一次的内容离开本机 —— 机密材料要自己拿捏。",
)

# ─────────────────── 本地模型：还没有的人怎么办 ───────────────────
#
# 这个工具的**默认状态是"没有模型也能用"**（提取、勾稽、估值全是纯代码）。
# 所以第一次打开的人不该看到一句"装 ollama"就没了 —— 要能照着做。

#: 三条路，各自的代价说在明面上。顺序 = 推荐顺序。
NO_MODEL_PATHS: tuple[dict, ...] = (
    {"title": "什么都不装（默认，也是最省事的）",
     "for": "只想拿到估值报告；问答那一步可以不要",
     "cost": "没有「读了材料回答问题」这个功能，其它一步不少",
     "how": "直接用：`python3 freeanalyst.py appraise <材料目录>`，"
            "或在网页向导里走完六步。**不需要任何模型。**"},
    {"title": "装一个本机模型（一次性的，之后一直离线可用）",
     "for": "想用问答、又不能让材料出本机",
     "cost": "要下载约 3~5GB（看档位），装一次；之后不联网也能用",
     "how": "三步：① 到 ollama.com 下载安装 Ollama（macOS/Windows/Linux 都有）；"
            "② 终端执行下面那条 `ollama pull`（按你的内存挑）；"
            "③ 回到这个页面点「检测」，或在终端跑 `python3 freeanalyst.py models`。"},
    {"title": "走云端（材料会离开本机）",
     "for": "本机实在跑不动，或者偶尔问一次、不想装东西",
     "cost": "**那一次的内容就离开了本机**（发到对方服务器）；每次调用都要你单独授权，并写进审计",
     "how": "在下面「云端模型」一节里填密钥、启用，然后每次调用都确认一次。"
            "机密材料自己拿捏 —— 工具不会替你判断。"},
)

#: 按内存挑模型。文件大小是 Q4 量化的**约数**（有实测的在注释里标了）。
RECOMMEND: tuple[dict, ...] = (
    {"ram_gb": 8, "model": "qwen3:4b", "download": "约 2.6GB",
     "why": "8GB 的机器留给系统的余量很小，4B 是本工具的问答能用的最小档"},
    {"ram_gb": 16, "model": "qwen3:8b", "download": "约 5GB",
     "why": "16GB 的商务本最稳的选择；本工具的问答够用"},
    {"ram_gb": 24, "model": "qwen3:14b", "download": "约 9.3GB",
     "why": "判断类问题更稳（实测在本机 16GB 上能跑，速度见配置页实测表）"},
    {"ram_gb": 32, "model": "qwen3:30b-a3b", "download": "约 18GB",
     "why": "MoE 每次只激活一部分参数，速度快，但**大不等于稳**，先过质量门槛"},
)

#: 装本机模型的三步 —— 命令行 `models` 和配置页共用这一段。
INSTALL_STEPS: tuple[str, ...] = (
    "① 装运行时：到 https://ollama.com 下载安装（macOS / Windows / Linux 都有），"
    "装完它会自动在后台跑一个只监听 127.0.0.1 的服务。",
    "② 拉一个模型：终端里执行 `ollama pull <模型名>`（按下面推荐的档位挑），"
    "等它下完。",
    "③ 回来确认：`python3 freeanalyst.py models`，或在配置页点「检测」。",
)


def detect_ram_gb() -> float:
    """这台机器多少内存 —— 用来给"你这台该装哪个模型"的建议。

    探不到就返回 0：**不猜**，页面会照实说"内存未知"。
    """
    import platform
    import subprocess

    try:
        if platform.system() == "Darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            return float(round(int(out) / 1024 ** 3)) if out.isdigit() else 0.0
        if platform.system() == "Linux":
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return float(round(kb / 1024 ** 2))
    except Exception:                                       # noqa: BLE001
        pass
    return 0.0


def recommend(ram_gb: float = 0.0) -> dict:
    """按内存给"该装哪个"，并给出**能直接粘的那条命令**。

    内存未知时给最小档并说明理由 —— 给"最不可能把人坑了"的那一档。
    """
    if not ram_gb:
        pick = RECOMMEND[1]                                  # 16GB 那档：最常见也最稳
        note = "内存没探到，先按最常见的 16GB 档建议；知道自己的内存就往上／往下挑一档。"
    else:
        pick = next((r for r in reversed(RECOMMEND) if ram_gb >= r["ram_gb"]),
                    RECOMMEND[0])
        note = f"你这台机器约 {ram_gb:.0f}GB 内存 → 建议 {pick['model']}。"
    q = quality_of(pick["model"])
    if q:
        note += f"（本机实测门槛 {q['score']}：{q['verdict']}）"
    return {**pick, "command": f"ollama pull {pick['model']}", "note": note,
            "quality": q}


# ─────────────── 质量门槛的实测成绩（不猜：没有文件就说没有） ───────────────
#: `bench/gate-*.json` —— 由 `bench/model_quality.py --json <路径>` 生成
GATE_FILES: tuple[Path, ...] = (ROOT / "bench" / "gate-local-6.json",
                                ROOT / "bench" / "gate-qwen35.json",
                                ROOT / "bench" / "gate-cloud-2.json")


def quality_rows() -> list[dict]:
    """读所有门槛成绩：模型 / 分数 / 裁定 / 卡在哪几项。"""
    rows: list[dict] = []
    for f in GATE_FILES:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                   # noqa: BLE001
            continue
        for model, r in (d or {}).items():
            failed = []
            for c in (r.get("results") or []):
                if not c.get("passed"):
                    failed.append(c.get("name") or c.get("id") or "?")
            # **"测不了"不是"不合格"。** 旧的结果文件里没记 error 字段时，
            # 全部用例都是"调用失败"的情况会被当成 0/5 质量裁定 —— 那是假裁定：
            # 人会据此把模型否掉，而问题在密钥或网络上。（与 `llm/gate.py` 同一判据）
            err = r.get("error", "")
            if not err:
                reasons = [(c.get("reasons") or []) for c in (r.get("results") or [])]
                if reasons and all(x and all("调用失败" in y for y in x) for x in reasons):
                    err = reasons[0][0]
            rows.append({"model": model,
                         "passed": r.get("passed", 0), "total": r.get("total", 0),
                         "score": f"{r.get('passed', 0)}/{r.get('total', 0)}",
                         "verdict": r.get("verdict", ""),
                         "error": err,
                         "failed": [] if err else failed,
                         "local": "/" not in model})
    return sorted(rows, key=lambda r: (-r["passed"], r["model"]))


def quality_of(model: str) -> dict:
    """某个模型的成绩（没有返回空 —— 页面照实说"没测过"）。"""
    for r in quality_rows():
        if r["model"] == model or r["model"].endswith("/" + model):
            return r
    return {}


def truth_lines() -> list[str]:
    """**最该先说清楚的一句话** —— 本地模型现在过不了门槛，云端能过。

    这条不是推理，是实测（`bench/gate-local-6.json` 与 `gate-cloud-2.json`）。
    指引里如果不写，用户会以为"装个 8B 就万事大吉"，然后拿到错的答案还照用。
    """
    rows = quality_rows()
    if not rows:
        return ["还没有质量门槛的实测成绩 —— 先跑 `bench/model_quality.py`。"]
    local = [r for r in rows if r["local"]]
    cloud = [r for r in rows if not r["local"]]
    best_local = max((r["passed"] for r in local), default=0)
    best_cloud = max((r["passed"] for r in cloud), default=0)
    total = (local[0]["total"] if local else 5)
    out = []
    if local and best_local < total:
        out.append(f"**本机模型目前没有一个过门槛**（门槛是 {total} 项全过，一票否决）："
                   f"最好的是 {best_local}/{total}。所以本机问答现阶段只能当**辅助** —— "
                   "答案必须带出处、关键结论要人工回原文核对。")
    if cloud and best_cloud == total:
        names = "、".join(sorted(r["model"] for r in cloud if r["passed"] == total))
        out.append(f"闭源云端模型里有过的（{best_cloud}/{total}）：{names}。"
                   "代价是那一次的内容离开本机 —— 按次授权、进审计，机密材料自己拿捏。")
    return out


def experience(tok_s: float) -> tuple[str, str]:
    """速度 → (档位名, 人话描述)。量不出来就给空，不猜。"""
    if not tok_s or tok_s <= 0:
        return "", ""
    for floor, name, words in SPEED_TIERS:
        if tok_s >= floor:
            return name, words
    return "", ""


def fits(resident_gb: float, ram_gb: float) -> tuple[bool, str]:
    """装不装得下 —— 留 4GB 给系统和别的程序（浏览器吃掉的内存往往比模型还多）。

    驻留与文件的比例：**实测 1.0~1.14 倍**（Q4 量化，见 `bench/measured-local.json`：
    8.6GB 的 qwen3:14b 驻留 9.4GB）。指引里写"1.1~1.3 倍"是保守估计，
    下面这张表按实测口径解释，留的余量仍然按 4GB 算。
    """
    if not resident_gb or not ram_gb:
        return True, "内存未知，不敢下结论"
    room = ram_gb - 4
    if resident_gb <= room:
        return True, f"装得下（占 {resident_gb}GB，可用约 {room:.0f}GB）"
    return False, f"装不下（要 {resident_gb}GB，可用约 {room:.0f}GB）"


def load_measured(path: Path | None = None) -> dict:
    """读本机实测。没有就返回空 —— **页面会照实说"还没量"**。"""
    p = Path(path) if path else MEASURED
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def measured_rows(path: Path | None = None) -> list[dict]:
    """实测表：每个模型 + 能不能跑 + 什么体验（结论是算出来的，不是抄的）。"""
    d = load_measured(path)
    ram = (d.get("machine") or {}).get("ram_gb") or 0
    rows = []
    for r in d.get("models") or []:
        if r.get("error"):
            rows.append({**r, "fits": False, "fit_note": r["error"],
                         "feel": "跑不起来", "feel_note": ""})
            continue
        ok, note = fits(r.get("resident_gb", 0), ram)
        feel, words = experience(r.get("tok_s", 0))
        rows.append({**r, "fits": ok, "fit_note": note,
                     "feel": feel, "feel_note": words})
    return rows


def summary_lines(path: Path | None = None) -> list[str]:
    """给命令行和说明文件用的一段话（和页面同一份数据）。"""
    d = load_measured(path)
    m = d.get("machine") or {}
    out = []
    if m:
        bits = [m.get("chip", ""), f"{m.get('ram_gb','?')}GB 内存" if m.get("ram_gb") else ""]
        out.append("实测机器：" + " · ".join(b for b in bits if b))
    rows = measured_rows(path)
    if not rows:
        out.append("本机实测还没跑过 —— 先 `python3 bench/measure_models.py "
                   "--json bench/measured-local.json`")
        return out
    for r in rows:
        if r.get("error"):
            out.append(f"  {r['name']}：{r['error']}")
        else:
            out.append(f"  {r['name']}：文件 {r.get('file_gb',0)}GB · "
                       f"驻留 {r.get('resident_gb',0)}GB（{r['fit_note']}）· "
                       f"{r.get('tok_s',0)} tok/s（{r['feel']}）")
    return out
