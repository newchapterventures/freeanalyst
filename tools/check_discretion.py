#!/usr/bin/env python3
"""**代码、模型、材料，三者严格分开**：这个仓库（公开）只放代码。

## 用户定的原则（2026-09-29，逐字）
> 要把代码、模型、材料严格且分开来。只有代码到 github

    ┌─────────┬──────────────────────────────────────────────┐
    │ 代码     │ ✅ 进仓库（公开）                              │
    │ 材料     │ ❌ 不进仓库 —— 只在分析师自己的机器 / NCV 服务器  │
    │ 模型     │ ❌ 不进仓库 —— 权重很大，而且跟代码不是一个生命周期 │
    └─────────┴──────────────────────────────────────────────┘

## 为什么要有这个检查（真踩过）
材料**文件**从来没进过仓库（`.gitignore` 一直挡着，实测历史里只有合成的测试 PDF）——
**但材料的身份**进去了：我把公司名写进注释和提交信息，跟着代码上了公开的 GitHub。
（那份还是**非上市**公司的审计报告。）

所以这条检查管两件事：
  1. **别把东西搬进来** —— 材料类文件、模型权重、大文件，一律不许被跟踪；
  2. **别把身份写进去** —— 非公开来源的材料名，注释/文档/提交信息里都不许出现。

## 判据
**写"哪类材料"，不写"哪一份"。**
    ✓ 一份单栏排版的扫描件 / 一家非上市制造企业 / 公开上市公司的年报
    ✗ 具体公司名（来源不公开的）

## 名单为什么是编码的
这个文件也在公开仓库里。名单要是明文，就等于**把要保护的名字又公开了一遍** ——
所以存的是 base64。维护（加/删名字）：`python3 tools/check_discretion.py --show-list`
"""
from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

#: 非公开来源的材料身份，base64 存放（见上文说明）。加名字前先问：
#: "它的财报外界拿得到吗？" 拿得到 → 不用加。
BANNED_B64 = (
    "6IuP5bee5LqV5Yip",
    "5LqV5Yip55S15a2Q",
    "5Y2O5aSP5Z+655+z",
    "5Lit55Ge5bKz5Y2O",
    "Rm9zdW4=",
    "6JOd6Imy5YWJ5qCH",
)

#: 材料类文件 —— 一个都不许被跟踪。**例外**在下面 WHITELIST 里，且必须在注释说明理由。
MATERIAL_SUFFIX = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"}

#: 模型权重 —— 体积大、且与代码不同生命周期，不进仓库。
MODEL_SUFFIX = {".gguf", ".ggml", ".safetensors", ".onnx", ".pt", ".pth", ".ckpt",
                ".h5", ".tflite", ".mlmodel", ".mlpackage", ".msgpack", ".bin"}

#: 任何被跟踪文件的大小上限。材料和模型都会撞到这条，是最后一道兜底。
MAX_BYTES = 2 * 1024 * 1024

#: 允许进仓库的少数"材料类"文件 —— 每一条都要能说清为什么无害。
WHITELIST = {
    # 合成的测试材料（由同名 .html 用浏览器渲染），不含任何真实信息
    "corpus/test-financials.pdf",
    # Fitbit 2016 10-K 的三个 htm 片段：**公开披露文件**，约 270KB，
    # 用作 HTML 抽取的测试固定件
    "materials/fitbit-2016-10k/R2.htm",
    "materials/fitbit-2016-10k/R4.htm",
    "materials/fitbit-2016-10k/R8.htm",
}


def banned() -> list[str]:
    return [base64.b64decode(s).decode() for s in BANNED_B64]


def tracked() -> list[str]:
    return subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                          text=True).stdout.splitlines()


def scan_names(text: str) -> list[tuple[int, str, str]]:
    """文本里有没有出现非公开来源的材料身份。"""
    out = []
    for i, ln in enumerate(text.splitlines(), 1):
        if SELF.name in ln:                         # 本文件的说明行（名单是编码的）
            continue
        for name in banned():
            if name in ln:
                out.append((i, name, ln.strip()[:120]))
    return out


def judge(rel: str, size: int) -> str | None:
    """一个文件该不该被跟踪 —— 返回理由（不该）或 None（可以）。

    抽成纯函数就是为了**能被自测真的调用**：不拿真仓库、只看入参。
    """
    if rel in WHITELIST:
        return None
    suffix = Path(rel).suffix.lower()
    if suffix in MATERIAL_SUFFIX:
        return f"{rel} —— 材料类文件不许进仓库（要留就加进 WHITELIST 并写明理由）"
    if suffix in MODEL_SUFFIX:
        return f"{rel} —— 模型权重不许进仓库（代码和模型分开）"
    if size > MAX_BYTES:
        mb = size / 1024 / 1024
        return (f"{rel} —— {mb:.1f} MB，超过 {MAX_BYTES // 1024 // 1024} MB 上限"
                f"（材料/模型都会撞到这条，是兜底）")
    return None


def scan_files() -> list[str]:
    """被跟踪的文件里，有没有材料类 / 模型权重 / 超大的。"""
    bad = []
    for rel in tracked():
        try:
            size = Path(REPO, rel).stat().st_size
        except OSError:
            continue
        why = judge(rel, size)
        if why:
            bad.append(why)
    return bad


def scan() -> tuple[list[str], list[tuple[str, int, str, str]]]:
    file_bad = scan_files()
    name_bad: list[tuple[str, int, str, str]] = []
    for rel in tracked():
        if Path(rel).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".svg", ".ico"}:
            continue
        try:
            text = Path(REPO, rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, name, ln in scan_names(text):
            name_bad.append((rel, i, name, ln))

    # 提交信息也上 GitHub，也算"仓库内容"
    msg = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=REPO,
                         capture_output=True, text=True).stdout
    for i, name, ln in scan_names(msg):
        name_bad.append(("(最后一条提交信息)", i, name, ln))
    return file_bad, name_bad


def self_test() -> int:
    """自测：每条规则都得真的会咬人 —— 咬不到就不许留在仓库里。

    ★ 第一版这里写的是"检查后缀名单里有没有 .gguf" —— 那是**同义反复**，
    永远通过，等于没测（"不许看起来能用"）。改成**真调 `judge()`**。
    """
    ok = True

    probe = "  #: 取值依据：实测 " + banned()[0] + " 2.28×"
    if scan_names(probe):
        print("  ✓ 名字规则：塞进去的名字被抓住了")
    else:
        print("  ✗ 名字规则：没抓住 —— 是摆设")
        ok = False

    cases = [
        ("材料（真材料）", judge("材料/审计报告.pdf", 1000), True),
        ("材料（白名单里的合成样本）", judge("corpus/test-financials.pdf", 1000), False),
        ("模型权重", judge("models/foo.gguf", 1000), True),
        ("模型权重（safetensors）", judge("x/model.safetensors", 1000), True),
        ("大文件（兜底）", judge("data/blob.txt", MAX_BYTES + 1), True),
        ("普通代码", judge("valuation/dupont.py", 20000), False),
    ]
    for label, got, want_flagged in cases:
        flagged = got is not None
        if flagged == want_flagged:
            print(f"  ✓ {label}：{'拦住了' if flagged else '放行'}（符合预期）")
        else:
            print(f"  ✗ {label}：期望{'拦住' if want_flagged else '放行'}，实际相反")
            ok = False

    print("✓ 自测通过：这个检查不是摆设" if ok else "✗ 自测失败")
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if "--show-list" in sys.argv:
        print("当前禁止名单（明文，仅供维护时看）：")
        for s in BANNED_B64:
            print(f"    {base64.b64decode(s).decode()}    {s}")
        return 0

    file_bad, name_bad = scan()
    if not file_bad and not name_bad:
        print(f"✓ 三个分开：被跟踪的 {len(tracked())} 个文件里"
              f"没有材料、没有模型权重、没有超限大文件；")
        print(f"  注释/文档/提交信息里也没有出现非公开来源的材料身份")
        return 0

    if file_bad:
        print(f"✗ {len(file_bad)} 个文件不该被跟踪（代码 / 模型 / 材料要分开）")
        for b in file_bad:
            print("    " + b)
    if name_bad:
        print(f"✗ {len(name_bad)} 处出现了非公开来源的材料身份（这个仓库是公开的）")
        for rel, i, name, ln in name_bad:
            print(f"    {rel}:{i}  「{name}」  {ln}")
    print('\n改法：材料与模型放在仓库**外面**；正文里写"哪类材料"，不写"哪一份"。')
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
