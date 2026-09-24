"""云端（闭源）大模型 —— **必须按次授权，配一次不是拿到通行证**。

## 与本机模型的分界

    本机模型（`backends.py`）  走 127.0.0.1        → `guard` 默认放行
    云端模型（本文件）         走外部主机          → `guard` **默认拦截**

要把内容发到外部主机，这次调用必须带一个 `guard.CloudConsent`，它绑定
**主机 + 用途 + 这一次到底发什么**（人话描述，让授权界面上看得懂），
而且**授权这件事本身也写进审计**（`audits/egress.jsonl`）。

刻意**没有** `ALLOW_CLOUD=1` 这类全局开关 —— 有它在，「机密材料不出网」
这句话当天就作废了（项目里反复写过这条）。

## 现在支持哪些 / 未来可以支持哪些

下面 `PROVIDERS` 一张表说清。两类：

* `ready`      —— OpenAI 兼容（`/v1/chat/completions`），**填个 key 就能用**
* `adapter`    —— 协议不兼容（Anthropic Messages / Gemini generateContent /
                  AWS Bedrock），**需要单独适配，现在没做** —— 不假装能用

本机模型那条路一个字节都不变；云端这条路**默认关闭**，要在配置页面显式打开，
并且每次调用都要有授权。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .backends import GenResult, ModelError

#: 服务商注册表。
#:   kind=ready   ：OpenAI 兼容，填 key 即可用
#:   kind=adapter ：协议不同，需要写适配器（**没做，不假装**）
#:   key_file     ：密钥从**本地文件**读（不把密钥写进配置、更不进仓库）
#:   models       ：常见模型名（云端列模型要发请求 = 要授权，所以给静态清单）
PROVIDERS: dict[str, dict] = {
    # ── 中国大陆（OpenAI 兼容）──
    "deepseek": {
        "label": "DeepSeek", "kind": "ready", "host": "api.deepseek.com",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_file": "~/.keys/Deepseek API Key.md",
    },
    "dashscope": {
        "label": "通义千问（阿里百炼 · 兼容模式）", "kind": "ready",
        "host": "dashscope.aliyuncs.com",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen3-max"],
        "key_file": "~/.keys/千问key.md",
    },
    "moonshot": {
        "label": "Kimi（月之暗面）", "kind": "ready", "host": "api.moonshot.cn",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k2-0905-preview", "moonshot-v1-128k"],
        "key_file": "",
    },
    "zhipu": {
        "label": "智谱 GLM", "kind": "ready", "host": "open.bigmodel.cn",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air"], "key_file": "",
    },
    "minimax": {
        "label": "MiniMax", "kind": "ready", "host": "api.minimax.chat",
        "base_url": "https://api.minimax.chat/v1",
        "models": ["abab6.5s-chat"], "key_file": "~/.keys/Minimax Lite Token Plan.md",
    },
    "ark": {
        "label": "火山方舟（豆包）", "kind": "ready", "host": "ark.cn-beijing.volces.com",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-1-6", "doubao-1-5-pro-32k"],
        "key_file": "~/.keys/火山方舟coding plan.md",
    },
    "siliconflow": {
        "label": "硅基流动（聚合开源模型）", "kind": "ready",
        "host": "api.siliconflow.cn", "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "key_file": "",
    },
    # ── 海外（OpenAI 兼容）──
    "openai": {
        "label": "OpenAI", "kind": "ready", "host": "api.openai.com",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-5", "gpt-4.1", "o4-mini"], "key_file": "",
    },
    "nvidia": {
        "label": "NVIDIA NIM（免费额度）", "kind": "ready",
        "host": "integrate.api.nvidia.com",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": ["deepseek-ai/deepseek-r1", "meta/llama-3.3-70b-instruct"],
        "key_file": "~/.keys/Nvidia免费模型key.md",
    },
    "xai": {
        "label": "xAI Grok", "kind": "ready", "host": "api.x.ai",
        "base_url": "https://api.x.ai/v1", "models": ["grok-4", "grok-3"],
        "key_file": "",
    },
    "mistral": {
        "label": "Mistral", "kind": "ready", "host": "api.mistral.ai",
        "base_url": "https://api.mistral.ai/v1",
        "models": ["mistral-large-latest"], "key_file": "",
    },
    "groq": {
        "label": "Groq（开源模型高速推理）", "kind": "ready",
        "host": "api.groq.com", "base_url": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile"], "key_file": "",
    },
    # ── 需要单独适配（**没做**）──
    "anthropic": {
        "label": "Anthropic Claude", "kind": "adapter", "host": "api.anthropic.com",
        "base_url": "https://api.anthropic.com/v1/messages",
        "models": ["claude-sonnet-4-5", "claude-opus-4-1"], "key_file": "",
        "note": "用 Messages 协议（不是 /v1/chat/completions），需要单独写适配器",
    },
    "gemini": {
        "label": "Google Gemini", "kind": "adapter", "host": "generativelanguage.googleapis.com",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"], "key_file": "",
        "note": "用 generateContent 协议，需要单独写适配器",
    },
    "bedrock": {
        "label": "AWS Bedrock", "kind": "adapter", "host": "bedrock-runtime.*.amazonaws.com",
        "base_url": "", "models": ["anthropic.claude-*", "meta.llama*"],
        "key_file": "", "note": "走 AWS 签名（SigV4），需要单独写适配器",
    },
}

#: 密钥文件里可能混着别的内容 —— 只挑出像密钥的那一段。
_KEY_PAT = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|nvapi-[A-Za-z0-9_\-]{16,}|"
                      r"[0-9a-f]{32}\.[A-Za-z0-9_\-]{16,})")


def read_key_file(path: str | Path) -> str:
    """从本地文件里取密钥。**文件整篇不读进日志、不回显给界面。**

    `~/.keys/*.md` 是使用者自己的凭据库（里面可能还有别的服务的信息），
    所以这里只按模式挑出密钥那一段，别的内容不碰、也不打印。
    """
    p = Path(os.path.expanduser(str(path)))
    if not str(path).strip() or not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = _KEY_PAT.search(text)
    return m.group(1) if m else ""


def mask(key: str) -> str:
    """界面上只显示这个 —— **密钥不回显**。"""
    if not key:
        return ""
    return key[:6] + "…" + key[-4:] if len(key) > 12 else "已配置"


@dataclass
class CloudBackend:
    """一个云端服务商（OpenAI 兼容协议）。**每次调用都要授权。**"""

    provider: str
    base_url: str
    api_key: str = ""
    host: str = ""
    name: str = ""
    models: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        info = PROVIDERS.get(self.provider, {})
        self.host = self.host or info.get("host", "")
        self.name = self.name or f"cloud:{self.provider}"
        self.models = self.models or list(info.get("models", []))

    def available(self) -> tuple[bool, str]:
        """有 key 才算可用。**没有 key 不是错误，是"还没配"。**"""
        if not self.api_key:
            return False, (f"没找到 {PROVIDERS.get(self.provider, {}).get('label')} 的密钥 ——"
                           "在配置页面填入，或把密钥文件路径写上")
        return True, f"已配置（{self.host}）"

    def list_models(self) -> list[str]:
        """静态清单。

        **不去云端列模型**：那要发一次请求 = 要一次授权，而列模型对估值没有用。
        少发一次多余的请求，就少一次把内容发出去的机会。
        """
        return list(self.models)

    def generate(self, model: str, prompt: str, system: str = "",
                 timeout: int = 120, *, consent=None,
                 purpose: str = "") -> GenResult:
        """发一次请求到云端 —— **没有 conset 直接拒绝，不发**。"""
        if consent is None:
            raise ModelError(
                f"拒绝调用云端模型 {self.provider}/{model}：**没有按次授权**。\n"
                "云端调用必须带一个 CloudConsent（绑定 主机 + 用途 + 这一次发什么），"
                "授权本身也会写进审计日志。这是这个工具的底线，不是可以关掉的开关。")
        if consent.host != self.host:
            raise ModelError(f"授权的主机是 {consent.host}，而这次要发给 {self.host} "
                             "—— 授权不匹配，拒绝发送")

        from guard import guarded_request
        msgs = ([{"role": "system", "content": system}] if system else [])
        msgs.append({"role": "user", "content": prompt})
        body = json.dumps({"model": model, "messages": msgs,
                           "temperature": 0.1, "stream": False}).encode("utf-8")
        raw = guarded_request(
            self.base_url.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            purpose=purpose or consent.purpose, timeout=timeout, consent=consent)
        data = json.loads(raw.decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        return GenResult(text=text, model=model, backend=self.name)


def build(provider: str, *, api_key: str = "", api_key_file: str = "",
          base_url: str = "") -> CloudBackend:
    """按配置建一个云端后端（密钥优先用直接填的，否则按文件路径读）。"""
    info = PROVIDERS.get(provider)
    if not info:
        raise ModelError(f"不认识的服务商 {provider!r}；"
                         f"支持的有：{'、'.join(PROVIDERS)}")
    if info.get("kind") != "ready":
        raise ModelError(f"{info['label']} 需要单独适配（{info.get('note', '')}）—— "
                         "**没有假装能用**，要用请先在 `llm/cloud.py` 里写适配器")
    key = api_key.strip() or read_key_file(api_key_file or info.get("key_file", ""))
    return CloudBackend(provider=provider,
                        base_url=base_url or info["base_url"], api_key=key)
