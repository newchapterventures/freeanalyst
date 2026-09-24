"""大模型配置的存放 —— **密钥不进仓库、界面不回显**。

## 放在哪

    ~/.freeanalyst/llm.json       （权限 600）

刻意不放在仓库里：密钥进了 git 就等于公开了，而且这个仓库是**开源**的。

## 记什么

    local    本机运行时（ollama / 任意 OpenAI 兼容服务）的地址与开关
    cloud    每个云端服务商：开关、密钥（或密钥文件路径）
    purposes 用途 → 用哪个模型（现在只有「问答」这一处真的在用模型）

## 一条纪律：**默认全关**

云端默认关、本机探测到才可用。配置里**没有**"允许全局出网"这种字段 ——
云端每一次调用都还要 `guard.CloudConsent` 按次授权（见 `llm/cloud.py`）。
配置文件只回答"你打算用谁"，不回答"你是否允许发出去"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path(os.path.expanduser("~/.freeanalyst/llm.json"))

#: 现在真的在用模型的**用途**（其余都是纯代码，不需要模型）。
PURPOSE_LABEL = {
    "ask": "材料问答（把材料建成索引后提问）",
}

DEFAULT: dict = {
    "version": 1,
    "local": {
        #: ollama 默认端口；**必须是回环**（guard 只放行 127.0.0.1）
        "ollama_url": "http://127.0.0.1:11434",
        #: LM Studio / vLLM / llama.cpp server 都提供 OpenAI 兼容接口
        "openai_compat_url": "http://127.0.0.1:1234",
        "openai_compat_enabled": False,
    },
    #: 每个服务商：enabled（默认全关）、api_key（留空则按 key_file 读）
    "cloud": {
        name: {"enabled": False, "api_key": "", "api_key_file": ""}
        for name in ("deepseek", "dashscope", "moonshot", "zhipu", "minimax",
                     "ark", "siliconflow", "openai", "nvidia", "xai",
                     "mistral", "groq", "anthropic", "gemini", "bedrock")
    },
    "purposes": {"ask": "qwen3:14b"},
}


def load(path: Path | None = None) -> dict:
    """读配置。**缺字段用默认补上**（加了新的服务商时旧配置不会缺项）。"""
    p = Path(path or DEFAULT_PATH).expanduser()
    cfg = json.loads(json.dumps(DEFAULT))          # 深拷贝
    if p.exists():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cfg                                # 坏了就用默认，不崩
        for key, val in saved.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                for k2, v2 in val.items():
                    if isinstance(v2, dict) and isinstance(cfg[key].get(k2), dict):
                        cfg[key][k2].update(v2)
                    else:
                        cfg[key][k2] = v2
            else:
                cfg[key] = val
    return cfg


def save(cfg: dict, path: Path | None = None) -> Path:
    """写配置。**目录 700、文件 600** —— 里面有密钥。"""
    p = Path(path or DEFAULT_PATH).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def masked(cfg: dict) -> dict:
    """给界面看的版本：**密钥只留打码**（界面永远拿不到原文）。"""
    from .cloud import mask, read_key_file

    out = json.loads(json.dumps(cfg))
    for name, c in (out.get("cloud") or {}).items():
        key = (c.get("api_key") or "").strip() or read_key_file(c.get("api_key_file") or "")
        c["api_key"] = ""
        c["key_display"] = mask(key)
        c["has_key"] = bool(key)
        c.pop("api_key", None)
    return out


def key_for(cfg: dict, provider: str) -> str:
    """取某个服务商实际要用的密钥（直接填的优先，否则按文件路径读）。"""
    from .cloud import PROVIDERS, read_key_file

    c = (cfg.get("cloud") or {}).get(provider) or {}
    return (c.get("api_key") or "").strip() or read_key_file(
        c.get("api_key_file") or PROVIDERS.get(provider, {}).get("key_file", ""))
