"""震荡策略 v1.0——第八课的箱体高抛低吸。

与 V5.8 的三条根本差别：

1. 箱体上下沿是"≥2 次触碰连成的水平线"（L8R2/R3），不是布林带轨道。
   布林带是统计带，价格靠近上轨不代表那里有阻力；水平沿是历史上真实被拒绝过的价位。
2. 止盈目标在开仓那一刻就锁死为对侧沿（L8R8），**不随后续 K 线移动**。
   V5.8 取的是当根 bb_upper，价格涨则中轨抬、σ 又放大，目标是双重后退的——
   这是十三节复盘里"49 小时 0 次对侧轨止盈"的疑似成因。
3. 箱体失效是**事前**的：实体收盘出沿即作废箱体并离场（L8R17），
   而不是 V5.8 那种打了止损才按品种封锁、要等很久才解除。
"""

from __future__ import annotations

import pandas as pd

from gold_crypto_quant.strategies_v1.engine import (
    BarContext,
    CloseIntent,
    Intent,
    OpenIntent,
)
from gold_crypto_quant.strategies_v1.indicators import Box, detect_box, volume_ratio
from gold_crypto_quant.strategies_v1.params import RangeParams


class RangeStrategyV1:
    """课程箱体震荡策略。"""

    name = "震荡"
    version = "1.0.0"

    def __init__(self, params: RangeParams | None = None) -> None:
        self.params = params or RangeParams()
        self.structure_interval = self.params.structure_interval
        self.execution_interval = self.params.execution_interval
        # 最近一次识别出的箱体，供 Web 展示当前判定；不参与决策。
        self.last_box: dict[str, Box | None] = {}
        self.last_reason: dict[str, str] = {}

    # ------------------------------------------------------------------

    def on_bar(self, ctx: BarContext) -> list[Intent]:
        p = self.params
        # 箱体画在结构周期上（L8R4：周期越大骗线越少）；
        # 触碰与成交价走执行周期（L12R4：到更小周期找精确开仓）。
        structure = ctx.bars.get(self.structure_interval)
        execution = ctx.bars.get(self.execution_interval)
        if structure is None or execution is None or execution.empty:
            return []
        if len(structure) < max(40, p.lookback_bars // 4):
            return []

        box = detect_box(
            structure,
            lookback=p.lookback_bars,
            swing_window=p.swing_window,
            tolerance=p.touch_tolerance,
            min_touches=p.min_touches,
            min_height=p.min_box_height,
            max_height=p.max_box_height,
            use_body=p.use_body_for_edges,
        )
        self.last_box[ctx.symbol] = box
        if box is None:
            self.last_reason[ctx.symbol] = "无合格箱体"
            return []

        # ---- 突破判定（L8R10/R11）：实体收盘出沿 + 放量 ----
        # 必须用结构周期最后一根**已收线**的K线。执行周期的一根 5m 实体出沿
        # 不构成 L8R10 的"有效突破"，那正是 L8R14 第一类假突破的样子。
        anchor = structure.iloc[-1]
        anchor_open, anchor_close = float(anchor["open"]), float(anchor["close"])
        body_high, body_low = max(anchor_open, anchor_close), min(anchor_open, anchor_close)
        broke_up = body_low > box.upper if p.breakout_body_close else anchor_close > box.upper
        broke_down = body_high < box.lower if p.breakout_body_close else anchor_close < box.lower
        if broke_up or broke_down:
            ratio = volume_ratio(structure, span=1, baseline=20)
            confirmed = ratio >= p.breakout_volume_ratio
            self.last_reason[ctx.symbol] = (
                f"箱体被{'向上' if broke_up else '向下'}突破"
                f"（量比{ratio:.2f}{'，确认' if confirmed else '，未放量'}）"
            )
            if ctx.position is not None and p.exit_on_breakout:
                return [CloseIntent(1.0, "箱体失效：实体收盘出沿")]
            return []

        if ctx.position is not None:
            self.last_reason[ctx.symbol] = "持仓中，箱体有效"
            return []

        # ---- 入场：在执行周期上判触碰与拒绝，成交价用执行周期的收盘 ----
        bar = execution.iloc[-1]
        close = float(bar["close"])
        low, high = float(bar["low"]), float(bar["high"])
        zone = box.height * p.entry_zone

        touched_lower = low <= box.lower + zone
        touched_upper = high >= box.upper - zone
        rejected_lower = close > box.lower if p.require_edge_rejection else True
        rejected_upper = close < box.upper if p.require_edge_rejection else True

        if touched_lower and rejected_lower and close < box.middle:
            stop = box.lower - box.height * p.stop_buffer
            target = box.upper if p.exit_at_opposite else 0.0
            self.last_reason[ctx.symbol] = f"下沿{box.lower:.2f}企稳做多"
            return [OpenIntent("LONG", stop, target, f"箱体下沿做多（触碰{box.lower_touches}次）")]

        if touched_upper and rejected_upper and close > box.middle:
            stop = box.upper + box.height * p.stop_buffer
            target = box.lower if p.exit_at_opposite else 0.0
            self.last_reason[ctx.symbol] = f"上沿{box.upper:.2f}受阻做空"
            return [OpenIntent("SHORT", stop, target, f"箱体上沿做空（触碰{box.upper_touches}次）")]

        self.last_reason[ctx.symbol] = f"箱体{box.lower:.2f}–{box.upper:.2f}，价格在中部等待"
        return []

    # ------------------------------------------------------------------

    def describe(self, symbol: str) -> dict:
        """给 Web 用的当前判定快照。"""
        box = self.last_box.get(symbol)
        return {
            "strategy": self.name,
            "version": self.version,
            "box": None
            if box is None
            else {
                "upper": round(box.upper, 4),
                "lower": round(box.lower, 4),
                "middle": round(box.middle, 4),
                "height": round(box.height, 4),
                "upper_touches": box.upper_touches,
                "lower_touches": box.lower_touches,
            },
            "reason": self.last_reason.get(symbol, ""),
        }
