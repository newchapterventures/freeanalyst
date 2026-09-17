"""数据源层 —— 可插拔的公开数据接入。

## 设计原则

**全部走 `net.guarded_get`（公开域闸门）。** 数据源层的代码拿不到本地语料，
这是架构保证，不是约定。

## 可插拔

标的可能在美国、中国、香港。数据源按市场注册：

| 市场 | 源 | 状态 |
|---|---|---|
| 美国上市公司 | SEC EDGAR（免费、无 key、官方） | ✅ 已接入 |
| A 股 / 港股 | AKShare / Tushare | ⬜ 待接入（需 pip install） |
| 股价（任何市场） | 待定 | ⬜ **缺口** |

## 已知缺口：EDGAR 没有股价

EDGAR 有财报，没有市值。所以：

- 能算：EBITDA、收入、净利、总资产、跨公司横向对比
- 不能算：市值、企业价值 EV、EV/EBITDA

要做市值倍数，必须再配一个价格源。**这一层留了位置，但没有假装它能用。**
"""

from . import comps_source, local_materials, prices, sec_edgar

__all__ = ["comps_source", "local_materials", "prices", "sec_edgar"]

#: 默认注册进来的可比公司源。
#: **注册 ≠ 可用** —— 每个源自己回答 `available()`，不能用的时候说清缺什么。
comps_source.register(local_materials.LocalMaterialsSource())
