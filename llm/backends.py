"""本机模型运行时的适配层 —— **用户装什么模型都能接上**。

## 为什么要有这一层

用户的要求（2026-09-18）：

> 类似于一个 app，用户下载到本地，然后再**适配相应的开源大模型**。

「适配」的意思是：用户本机上跑的可能是 ollama、可能是 LM Studio、
可能是 vLLM、也可能什么都没装 —— 这些都不该是能不能用这个工具的前提。

## 一条关键性质：**模型可以是空的**

这个工具里：

    提取三张表      纯代码（正则 + 版面分析 + OCR）
    口径判断        纯代码
    估值计算        **纯代码算，不用 LLM 算**（项目纪律）
    可比公司取数     结构化接口

大模型只用在**「读了材料回答问题」**和**「理解业务描述」**这几处 ——
都是**增强**，不是前提。

所以：

    没装模型 → 提取和估值照跑，只有「问答」和「行业建议」降级/不可用
    装了模型 → 全部可用，而且**装完先过质量门槛**（用户明确要求）

## 走哪条网络

所有请求都经 `guard.guarded_request()` —— 它**只放行 127.0.0.1**。
所以「模型在本机」不是一句承诺，是**代码里挡着的**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol


class ModelError(RuntimeError):
    pass


@dataclass
class GenResult:
    text: str
    model: str
    backend: str
    #: 是否被 guard 拦过（拦了就不该有结果，这里留痕）
    blocked: bool = False


class Backend(Protocol):
    """模型运行时的接口。实现 `available` / `list_models` / `generate` 就能插进来。"""

    name: str

    def available(self) -> tuple[bool, str]:
        """能不能用。不能用要说清**缺什么**（没装？没启动？端口不对？）。"""
        ...

    def list_models(self) -> list[str]:
        ...

    def generate(self, model: str, prompt: str, system: str = "",
                 timeout: int = 120) -> str:
        ...


# ─────────────────────────── Ollama ───────────────────────────

#: ollama 默认端口。**必须是回环地址** —— guard 只放行 127.0.0.1。
OLLAMA_URL = "http://127.0.0.1:11434"


@dataclass
class OllamaBackend:
    """Ollama —— 本机跑开源模型最常见的方式，也是这个项目的默认。"""

    base_url: str = OLLAMA_URL
    name: str = "ollama"

    def _get(self, path: str, purpose: str) -> bytes:
        from guard import guarded_request
        return guarded_request(self.base_url + path, purpose=purpose)

    def available(self) -> tuple[bool, str]:
        try:
            self._get("/api/tags", purpose="检测 ollama 是否在跑")
        except Exception as exc:                              # noqa: BLE001
            return False, (f"连不上 {self.base_url} —— 装了吗？跑起来了吗？"
                           f"（先在终端执行 `ollama serve`）｜{exc}")
        return True, "在跑"

    def list_models(self) -> list[str]:
        ok, why = self.available()
        if not ok:
            raise ModelError(why)
        raw = self._get("/api/tags", purpose="列本机模型")
        return [m["name"] for m in json.loads(raw.decode()).get("models", [])]

    def generate(self, model: str, prompt: str, system: str = "",
                 timeout: int = 120) -> str:
        from guard import guarded_request
        body = json.dumps({
            "model": model, "prompt": prompt, "system": system, "stream": False,
        }).encode()
        raw = guarded_request(self.base_url + "/api/generate", data=body,
                              purpose=f"问本机模型 {model}", timeout=timeout)
        return json.loads(raw.decode()).get("response", "")


# ──────────────── 任何 OpenAI 兼容的本地服务 ────────────────

#: 常见的本地 OpenAI 兼容端口（都是回环）
OPENAI_COMPAT_PORTS = (1234, 8000, 8080, 5000)


@dataclass
class OpenAICompatBackend:
    """LM Studio / vLLM / llama.cpp server —— 都提供 `/v1/chat/completions`。

    用户装的是这些的话，填个端口就能接上。
    """

    base_url: str = "http://127.0.0.1:1234"
    api_key: str = "local"          # 本机服务通常不校验，但字段要有
    name: str = "openai-compat"

    def available(self) -> tuple[bool, str]:
        try:
            from guard import guarded_request
            guarded_request(self.base_url + "/v1/models", purpose="检测本地兼容服务")
        except Exception as exc:                              # noqa: BLE001
            return False, f"连不上 {self.base_url}｜{exc}"
        return True, "在跑"

    def list_models(self) -> list[str]:
        from guard import guarded_request
        raw = guarded_request(self.base_url + "/v1/models", purpose="列本机模型")
        return [m["id"] for m in json.loads(raw.decode()).get("data", [])]

    def generate(self, model: str, prompt: str, system: str = "",
                 timeout: int = 120) -> str:
        from guard import guarded_request
        msgs = ([{"role": "system", "content": system}] if system else [])
        msgs.append({"role": "user", "content": prompt})
        body = json.dumps({"model": model, "messages": msgs}).encode()
        raw = guarded_request(
            self.base_url + "/v1/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            purpose=f"问本机模型 {model}", timeout=timeout)
        return json.loads(raw.decode())["choices"][0]["message"]["content"]


# ─────────────────────────── 注册表 ───────────────────────────

REGISTRY: dict[str, Backend] = {}


def register(backend: Backend, *, replace: bool = False) -> None:
    """注册一个模型运行时。**同名默认拒绝覆盖** —— 静默替换会让人
    以为用的是自己接的那个，实际被别人顶掉了。"""
    if backend.name in REGISTRY and not replace:
        raise ValueError(f"{backend.name!r} 已注册；要覆盖请显式 replace=True")
    REGISTRY[backend.name] = backend


def detect() -> list[tuple[str, bool, str]]:
    """扫一遍本机有哪些模型运行时在跑。"""
    out = []
    for name, b in REGISTRY.items():
        try:
            ok, why = b.available()
        except Exception as exc:                              # noqa: BLE001
            ok, why = False, f"{type(exc).__name__}: {exc}"
        out.append((name, ok, why))
    return out


def pick() -> Backend | None:
    """挑一个能用的。没有就返回 None —— **没有模型不是错误**。"""
    for b in REGISTRY.values():
        try:
            ok, _ = b.available()
        except Exception:                                     # noqa: BLE001
            ok = False
        if ok:
            return b
    return None


register(OllamaBackend())
register(OpenAICompatBackend())
