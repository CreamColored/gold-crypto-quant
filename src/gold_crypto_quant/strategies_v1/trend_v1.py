"""顺势策略 v1.0——第一、七、十、十二课的顺势框架。

和震荡策略正好相反：那个赌价格回到区间内，这个赌价格继续沿原方向走。
两者的分工由趋势判定决定，这也是课程自己的答案（L8R4/L9R15/L12R9）。

四层过滤，缺一不进场：

  方向  大周期（默认 1h）按 L1R4/R5 判出明确趋势，高点与低点必须同时递增/递减
  强弱  DIF 与交易方向在零轴同侧（L10R6/R11）——水下做多只是弱势反弹
  位置  小周期（默认 15m）出现逆势回调，且回撤幅度落在 [min, max] 区间内
  量价  回调段缩量、转向根放量（L7R8/L7R12）

与 V5.8 最大的结构差别：V5.8 的 ENTRY_INTERVAL_PRIORITY 是**平铺优先级**，
15m 合格就开、不问 1h 什么方向；这里是**层级**，小周期必须服从大周期（L12R9）。
"""

from __future__ import annotations

import pandas as pd

from gold_crypto_quant.strategies_v1.engine import (
    BarContext,
    CloseIntent,
    Intent,
    MoveStopIntent,
    OpenIntent,
)
from gold_crypto_quant.strategies_v1.indicators import (
    MACD_SLOW,
    average_true_range,
    classify_trend,
    histogram_waves,
    macd_frame,
)
from gold_crypto_quant.strategies_v1.params import TrendParams


class TrendStrategyV1:
    """课程顺势策略。"""

    name = "顺势"
    version = "1.0.0"

    def __init__(self, params: TrendParams | None = None) -> None:
        self.params = params or TrendParams()
        self.last_reason: dict[str, str] = {}
        self.last_direction: dict[str, int] = {}

    # ------------------------------------------------------------------

    def on_bar(self, ctx: BarContext) -> list[Intent]:
        p = self.params
        entry_bars = ctx.bars.get(p.entry_interval)
        direction_bars = ctx.bars.get(p.direction_interval)
        # 执行周期只用来给成交价和跟踪止损的 ATR，形态判定一律在战术周期上做。
        exec_bars = ctx.bars.get(p.execution_interval)
        if entry_bars is None or direction_bars is None or exec_bars is None:
            return []
        if exec_bars.empty:
            return []
        # 门槛按判定实际需要推导，不写死。MACD 要 26 根才稳定，浪形判定再要
        # min_waves × min_wave_bars 根。写死 60 会让 7 天窗口里 1h 的前 36% 不可用。
        need = MACD_SLOW + p.min_waves * p.min_wave_bars + 4
        if len(entry_bars) < need or len(direction_bars) < need:
            return []

        # ---- 第一层：大周期定方向（L12R2）----
        verdict = classify_trend(
            direction_bars,
            min_waves=p.min_waves,
            min_wave_bars=p.min_wave_bars,
            lookback=p.trend_lookback,
        )
        self.last_direction[ctx.symbol] = verdict.direction

        if ctx.position is not None:
            return self._manage(ctx, entry_bars, exec_bars, verdict.direction)

        if p.require_direction_agreement and verdict.direction == 0:
            self.last_reason[ctx.symbol] = f"{p.direction_interval}无趋势：{verdict.reason}"
            return []
        direction = verdict.direction
        if direction == 0:
            return []

        # ---- 第二层：零轴强弱（L10R6/R11）----
        entry_macd = macd_frame(entry_bars)
        dif_now = float(entry_macd.dif.iloc[-1])
        if p.require_zero_axis and dif_now * direction <= 0:
            self.last_reason[ctx.symbol] = (
                f"{'多' if direction > 0 else '空'}头方向但 DIF={dif_now:.2f} 在零轴另一侧，"
                "按 L10R9 只算弱势反弹"
            )
            return []

        # ---- 第三层：小周期回调到位 ----
        waves = histogram_waves(entry_macd.histogram, min_bars=p.min_wave_bars)
        if len(waves) < 3:
            self.last_reason[ctx.symbol] = "小周期浪形不足"
            return []

        # 顺势方向的浪 → 逆势回调浪 → 当前刚转回顺势方向，就是入场那一刻。
        current, pullback, impulse = waves[-1], waves[-2], waves[-3]
        if current[0] != direction or pullback[0] == direction or impulse[0] != direction:
            self.last_reason[ctx.symbol] = "小周期未形成「顺势浪→回调浪→转回」的序列"
            return []
        # 只在转向后的头两根内进场；错过就不追（L11R11 禁止追涨杀跌）。
        if current[2] - current[1] + 1 > 2:
            self.last_reason[ctx.symbol] = "已转回顺势但超过 2 根，不追"
            return []

        close = entry_macd.close.to_numpy(dtype=float)
        impulse_start = float(close[impulse[1]])
        impulse_end = float(close[impulse[2]])
        span = abs(impulse_end - impulse_start)
        if span <= 0:
            return []
        pullback_extreme = (
            float(close[pullback[1] : pullback[2] + 1].min())
            if direction > 0
            else float(close[pullback[1] : pullback[2] + 1].max())
        )
        retrace = abs(impulse_end - pullback_extreme) / span
        if not p.pullback_min <= retrace <= p.pullback_max:
            self.last_reason[ctx.symbol] = (
                f"回撤 {retrace:.0%} 不在 [{p.pullback_min:.0%}, {p.pullback_max:.0%}] 区间"
            )
            return []

        # ---- 第四层：量价（L7R8/L7R12）----
        if p.require_volume and "volume" in entry_bars.columns:
            volume = pd.to_numeric(entry_bars["volume"], errors="coerce").astype(float)
            impulse_volume = float(volume.iloc[impulse[1] : impulse[2] + 1].mean())
            pullback_volume = float(volume.iloc[pullback[1] : pullback[2] + 1].mean())
            turn_volume = float(volume.iloc[current[1] : current[2] + 1].mean())
            if impulse_volume > 0 and pullback_volume / impulse_volume > p.volume_shrink_ratio:
                self.last_reason[ctx.symbol] = (
                    f"回调未缩量（{pullback_volume / impulse_volume:.2f} > "
                    f"{p.volume_shrink_ratio}），L7R8 不成立"
                )
                return []
            if pullback_volume > 0 and turn_volume / pullback_volume < p.volume_expand_ratio:
                self.last_reason[ctx.symbol] = (
                    f"转向根未放量（{turn_volume / pullback_volume:.2f} < {p.volume_expand_ratio}）"
                )
                return []

        # ---- 下单：止损放回调极值之外，留 ATR 缓冲（L13R6/R8）----
        # ATR 取战术周期：执行周期的 ATR 小一个量级，拿它做缓冲等于止损贴着价格放，
        # 一根正常的回踩就会被扫掉。L13R8 要的是"关键位下方留缓冲"，不是最小缓冲。
        atr = average_true_range(entry_bars, period=p.atr_period)
        if atr <= 0:
            return []
        price = float(exec_bars["close"].iloc[-1])
        buffer = atr * p.stop_atr_multiple
        stop = pullback_extreme - buffer if direction > 0 else pullback_extreme + buffer
        if (direction > 0 and stop >= price) or (direction < 0 and stop <= price):
            self.last_reason[ctx.symbol] = "止损位已被价格穿过，放弃这次入场"
            return []
        risk = abs(price - stop)
        target = 0.0
        if p.take_profit_r > 0:
            target = price + risk * p.take_profit_r * direction

        side = "LONG" if direction > 0 else "SHORT"
        self.last_reason[ctx.symbol] = (
            f"{p.direction_interval}{'多' if direction > 0 else '空'}头 + "
            f"{p.entry_interval}回撤{retrace:.0%}转回"
        )
        return [
            OpenIntent(
                side, stop, target,
                f"顺势入场：{p.direction_interval}{verdict.reason}，回撤{retrace:.0%}",
            )
        ]

    # ------------------------------------------------------------------

    def _manage(
        self, ctx: BarContext, entry_bars: pd.DataFrame,
        exec_bars: pd.DataFrame, direction: int,
    ) -> list[Intent]:
        """持仓管理：趋势破坏离场 + 跟踪止损。"""
        p = self.params
        position = ctx.position
        assert position is not None
        held = 1 if position.side == "LONG" else -1

        # L12R15：大周期方向反转，原策略前提消失，立即离场。
        if p.exit_on_trend_break and direction != 0 and direction != held:
            self.last_reason[ctx.symbol] = "大周期趋势反向，按 L12R15 离场"
            return [CloseIntent(1.0, "趋势反向")]

        risk = position.risk_distance
        if risk <= 0:
            return []
        price = float(exec_bars["close"].iloc[-1])
        gain_r = (price - position.entry_price) * held / risk
        if gain_r < p.trail_start_r:
            return []

        atr = average_true_range(entry_bars, period=p.atr_period)
        if atr <= 0:
            return []
        extreme = position.peak_price if held > 0 else position.trough_price
        trail = extreme - atr * p.trail_atr_multiple * held
        improved = (held > 0 and trail > position.stop_price) or (
            held < 0 and trail < position.stop_price
        )
        if improved:
            self.last_reason[ctx.symbol] = f"盈利{gain_r:.1f}R，跟踪止损上移"
            return [MoveStopIntent(trail, f"跟踪止损（{gain_r:.1f}R）")]
        return []

    # ------------------------------------------------------------------

    def describe(self, symbol: str) -> dict:
        direction = self.last_direction.get(symbol, 0)
        return {
            "strategy": self.name,
            "version": self.version,
            "direction": {1: "上升", -1: "下降", 0: "无趋势"}[direction],
            "reason": self.last_reason.get(symbol, ""),
        }
