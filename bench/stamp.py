"""给每次跑的结果盖**版本戳** —— 迟到的成绩单必须能对上「这是哪一版跑的」。

## 为什么要有这个（真踩过两次）

第一次是模型评测：一份格式规整的成绩单，用的是**已经被推翻的判据**，
跑完之后才飘过来。第二次是解析成功率量测：先跑的那版函数名写错、全报错，
可它**打印得比正确那版还详细**（带完整文件名），**更容易被当真**。

两次的共同点：

    **输出格式完全正常，只是版本过期。肉眼看不出，因为它长得跟对的一模一样。**

长跑任务天然有这个窗口 —— 我在这边跑、结论在那头等，中间隔着几分钟到几十分钟。
**这期间脚本被改过，成绩单就作废了，但它自己不知道。**

## 指纹怎么算

**把脚本内容本身揉进去**（不是版本号、不是时间戳）：

    sha256(文件名 + 文件内容)  →  改一个字符就变

所以「脚本改过」和「结果过期」是同一件事，不需要人去记版本号。

⚠️ **必须把判据所在的文件也一起揉进去。** 量测脚本常常只是壳子，
真正定义「什么算对」的常数在别的模块里（比如 `ingest/ocr.py` 的 `_STRICT_AMOUNT`）——
只哈希壳子的话，判据改了、指纹不变，这道闸等于没有。所以有 `deps`。

## 用法

    from bench import stamp

    result = {"rows": [...]}
    stamp.attach(result, __file__, deps=["../ingest/ocr.py"], pages=1610)
    path.write_text(json.dumps(result))

    # 迟到通知来了，核对一下：
    ok, why = stamp.check(payload, __file__, deps=["../ingest/ocr.py"])

命令行：

    python3 bench/stamp.py show  <结果.json>
    python3 bench/stamp.py check <结果.json> <脚本.py> [--deps 依赖.py ...]
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

#: 结果里放戳的那个键。用下划线开头，跟业务字段分开。
STAMP = "_stamp"


def _as_paths(items: Iterable[str | Path]) -> list[Path]:
    return [Path(x) for x in items]


def fingerprint(paths: Sequence[Path]) -> str:
    """把若干文件的**内容**揉成一个指纹。缺文件不静默跳过 —— 缺了就报。"""
    h = hashlib.sha256()
    missing: list[str] = []
    for p in sorted(_as_paths(paths), key=lambda x: (x.name, str(x))):
        try:
            body = p.read_bytes()
        except OSError:
            missing.append(p.name)
            continue
        h.update(p.name.encode("utf-8"))
        h.update(body)
    if missing:
        # **缺文件绝不能静默略过** —— 那会让指纹少算一个文件，而结果看起来正常。
        raise FileNotFoundError(f"算指纹时这些文件读不到：{', '.join(missing)}")
    return h.hexdigest()[:16]


def make(script: str | Path,
         deps: Sequence[str | Path] = (),
         **params: Any) -> dict:
    """造一个戳。`deps` 是**判据所在的文件**，必须带上。"""
    files = [Path(script), *_as_paths(deps)]
    return {
        "script": Path(script).name,
        "fingerprint": fingerprint(files),
        "hashed": sorted(p.name for p in files),
        "params": dict(params),
        "python": platform.python_version(),
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def attach(result: dict, script: str | Path,
           deps: Sequence[str | Path] = (), **params: Any) -> dict:
    """把戳挂到结果上（原地改，也返回它）。"""
    result[STAMP] = make(script, deps, **params)
    return result


def show(payload: dict) -> str:
    """人话描述这枚戳。"""
    st = (payload or {}).get(STAMP)
    if not st:
        return ("**这份结果没有版本戳。** 分不出是哪一版跑的 —— "
                "不能当作「当前版本的成绩」用。")
    ps = "　".join(f"{k}={v}" for k, v in (st.get("params") or {}).items())
    return (f"{st.get('script')}　指纹 {st.get('fingerprint')}　"
            f"{st.get('at')}　Python {st.get('python')}"
            + (f"　{ps}" if ps else "")
            + f"　（哈希了：{'、'.join(st.get('hashed') or [])}）")


def check(payload: dict, script: str | Path | None = None,
          deps: Sequence[str | Path] = ()) -> tuple[bool, str]:
    """核对一份结果还能不能当「当前版本」用。

    返回 `(还作数吗, 为什么)`。**说不清就问，不默认通过。**
    """
    st = (payload or {}).get(STAMP)
    if not st:
        return False, ("没有版本戳 —— 分不出是哪一版跑的。"
                       "**不能当作当前版本的结果用**；要嘛重跑，要嘛明确标注「来源版本未知」。")
    if script is None:
        return True, f"有戳但没拿当前脚本对：{show(payload)}"

    now = make(script, deps)
    if now["fingerprint"] == st.get("fingerprint"):
        return True, f"作数 ✓ {show(payload)}"

    # 指纹不同 —— 说清是**哪一类**不同，别只说「不一致」
    if sorted(now["hashed"]) != sorted(st.get("hashed") or []):
        why = (f"参与哈希的文件不一样：当时 {st.get('hashed')}，现在 {now['hashed']}")
    else:
        why = (f"文件内容变了（指纹 {st.get('fingerprint')} → {now['fingerprint']}）"
               f"：{'、'.join(now['hashed'])} 里有东西被改过")
    return False, (f"**这份结果作废。** {why}。"
                   f"当时 {st.get('at')} 跑的，之后代码改过 —— "
                   f"要嘛重跑，要嘛别拿它当当前成绩。")


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, path = argv[0], Path(argv[1])
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读不了 {path}：{type(exc).__name__}: {exc}")
        return 2

    # 结果可能是单个 dict，也可能是 list（多条记录共用一枚戳）
    first = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(first, dict):
        print("这份结果没有版本戳。")
        return 1

    if cmd == "show":
        print(show(first))
        return 0

    if cmd == "check":
        if len(argv) < 3:
            print("用法：stamp.py check <结果.json> <脚本.py> [--deps a.py b.py]")
            return 2
        script = argv[2]
        deps: list[str] = []
        if "--deps" in argv:
            deps = argv[argv.index("--deps") + 1:]
        ok, why = check(first, script, deps)
        print(("✓ " if ok else "✗ ") + why)
        return 0 if ok else 1

    print(f"不认识的命令：{cmd}（可用：show / check）")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
