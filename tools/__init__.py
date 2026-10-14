"""工具层 —— 把已有能力包成模型能调的东西。

    registry.py    注册表：名字 + 说明 + 参数 Schema + **副作用分级**
    catalog.py     具体工具：把 intake / valuation / datasources 接出来

## 这一层不实现任何算术

`call()` 路径里没有一行计算逻辑 —— 工具只是把已有函数接出来。
**模型可以「决定调用什么」，不能「参与计算」。** 包一层就不破坏它。

## 副作用分级是这一层的核心

    read_local   纯读本机   → 模型可自由调用
    egress       会访问外网  → **只对按次授权名单里的主机会拦**
    write        写盘/改状态 → 要用户点头

分级不是新造闸门，是给已有的闸门贴标签（`net.host_needs_consent`）。

## ⚠️ 每个工具的说明必须写「什么时候**别用**」

实测（`bench/model_choice.py`）：模型**选错工具**远少于**在不该用时伸手**。
`tests/test_tool_registry.py` 里有一条专门盯着这件事。
"""

from . import registry, catalog  # noqa: F401

__all__ = ["registry", "catalog"]
