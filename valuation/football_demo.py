"""Football Field 演示 —— 生成一张图看数据结构和渲染对不对。

⚠️ **区间数值是演算数据**，不是估值结论。
   真实股数（1,252,270,215）和基准价来自材料；
   各方法的区间宽度是为了验证渲染用的。
   真正的图要等：①可比公司倍数 ②DCF 假设（要跟用户确认）。

跑法：python3 -m valuation.football_demo
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valuation import football as fb


def demo() -> fb.FootballField:
    shares = 1_252_270_215          # 来自资产负债表「实收资本(或股本)」
    price = 1_480.0                 # 基准价（演示值）

    bands = [
        fb.Band("DCF", low=1_180.0, mid=1_395.0, high=1_640.0,
                basis="FCFF 折现，WACC 8.5%–10.5%，永续增长 2.5%–3.5%",
                sources=["合并利润表", "合并现金流量表", "附注：折旧摊销"],
                confidence="中",
                note="区间取敏感性网格极值，不是点值"),
        fb.Band("可比公司", low=1_240.0, mid=1_410.0, high=1_620.0,
                basis="EV/EBITDA 18x–24x 对应每股",
                sources=["可比公司市值（基准日）", "可比公司 EBITDA"],
                confidence="中"),
        fb.Band("可比交易", low=1_300.0, mid=1_520.0, high=1_780.0,
                basis="近三年 M&A 交易 EV/EBITDA 中位数",
                sources=["公告交易对价", "交易标的 EBITDA"],
                confidence="低",
                note="可比交易样本少，且控股权溢价口径不一"),
        fb.Band("净资产法", low=195.0, mid=215.0, high=236.0,
                basis="归母净资产 244,637,811,032.18 ÷ 股本，加一定溢价",
                sources=["合并资产负债表第 58 页"],
                confidence="低",
                note="白酒公司净资产与盈利能力脱节，仅作下限参考"),
        fb.Band("LBO", note="**没有借款科目** —— 标的账上没有有息负债，"
                            "杠杆收购的前提不成立，这个方法在这里不适用"),
        fb.Band("反向 VC", note="后期项目才用，不适用于已上市公司"),
    ]

    return fb.build(
        entity="某白酒公司（演示）",
        bands=bands,
        shares=shares,
        reference=price,
        reference_label="基准日收盘价",
        as_of="2025-12-31",
        notes=["**区间为演算数据**，用于验证数据结构与渲染；不是估值结论",
               "股数与基准价来自材料"],
    )


def main() -> int:
    ff = demo()
    out = Path(__file__).resolve().parent.parent / "out" / "football-demo.html"
    out.parent.mkdir(exist_ok=True)
    fb.render_html(ff, out)
    fb.save_json(ff, out.with_suffix(".json"))
    print(ff.render_text())
    print(f"\n  HTML -> {out}")
    print(f"  JSON -> {out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
