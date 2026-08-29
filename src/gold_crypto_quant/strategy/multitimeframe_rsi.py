"""从两个旧系统提取的多周期RSI纯信号；本模块不包含任何下单能力。"""

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd


@dataclass(frozen=True, slots=True)
class MultiTimeframeRsiParameters:
    """RSI方向、回撤和极值确认阈值。"""

    rsi_period: int = 14
    trend_long_threshold: float = 55.0
    trend_short_threshold: float = 45.0
    confirmation_long_max: float = 48.0
    confirmation_short_min: float = 52.0
    trigger_long_extreme: float = 22.0
    trigger_short_extreme: float = 78.0
    trigger_long_recovery: float = 30.0
    trigger_short_recovery: float = 70.0

    def __post_init__(self) -> None:
        if self.rsi_period < 2:
            raise ValueError("rsi_period must be at least 2")
        thresholds = (
            self.trend_long_threshold,
            self.trend_short_threshold,
            self.confirmation_long_max,
            self.confirmation_short_min,
            self.trigger_long_extreme,
            self.trigger_short_extreme,
            self.trigger_long_recovery,
            self.trigger_short_recovery,
        )
        if any(value < 0 or value > 100 for value in thresholds):
            raise ValueError("RSI thresholds must be in [0, 100]")


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """使用Wilder平滑计算RSI。"""
    change = close.diff()
    gain = change.clip(lower=0.0)
    loss = -change.clip(upper=0.0)
    # 调用指数加权均值模拟Wilder递推；预热不足时保持NaN，避免早期虚假信号。
    average_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    relative_strength = average_gain / average_loss.replace(0.0, float("nan"))
    result = 100.0 - 100.0 / (1.0 + relative_strength)
    return result.where(average_loss.ne(0.0), 100.0)


def _closed_and_aligned(
    values: pd.Series,
    *,
    duration: timedelta,
    target_index: pd.DatetimeIndex,
) -> pd.Series:
    """把以开盘时间标记的指标移动到收盘时刻，再对齐至低周期。"""
    closed = values.copy()
    closed.index = closed.index + duration
    # 调用前向填充时，目标时刻只能获得已经收盘的最后一根高周期指标。
    return closed.reindex(target_index, method="ffill")


def generate_multitimeframe_rsi_signals(
    trigger_bars: pd.DataFrame,
    confirmation_bars: pd.DataFrame,
    trend_bars: pd.DataFrame,
    parameters: MultiTimeframeRsiParameters | None = None,
) -> pd.DataFrame:
    """按1分钟触发、5分钟回撤、1小时方向生成下一根1分钟执行信号。

    三组K线索引都必须使用UTC开盘时间。该函数只做历史研究；当前项目规定的最低
    运行周期是5分钟，因此它不会被纸面交易或策略准入流程自动调用。
    """
    parameters = parameters or MultiTimeframeRsiParameters()
    frames = {
        "trigger": trigger_bars,
        "confirmation": confirmation_bars,
        "trend": trend_bars,
    }
    for name, frame in frames.items():
        if "close" not in frame.columns or frame.empty:
            raise ValueError(f"{name} bars must contain non-empty close prices")
        if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
            raise ValueError(f"{name} bars index must be timezone-aware")
        if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            raise ValueError(f"{name} bars index must be sorted and unique")

    trigger_close = pd.to_numeric(trigger_bars["close"], errors="raise").astype(float)
    confirmation_close = pd.to_numeric(
        confirmation_bars["close"], errors="raise"
    ).astype(float)
    trend_close = pd.to_numeric(trend_bars["close"], errors="raise").astype(float)
    # 分别调用RSI计算方法；高周期结果随后只在对应K线收盘后才向1分钟传播。
    trigger_rsi = _rsi(trigger_close, parameters.rsi_period)
    confirmation_rsi = _closed_and_aligned(
        _rsi(confirmation_close, parameters.rsi_period),
        duration=timedelta(minutes=5),
        target_index=trigger_bars.index,
    )
    trend_rsi = _closed_and_aligned(
        _rsi(trend_close, parameters.rsi_period),
        duration=timedelta(hours=1),
        target_index=trigger_bars.index,
    )

    # 极值必须先出现，随后本根重新穿越恢复阈值；这样不会把“仍在加速下跌”当成做多点。
    long_recovery = (trigger_rsi.shift(1) <= parameters.trigger_long_extreme) & (
        trigger_rsi >= parameters.trigger_long_recovery
    )
    short_recovery = (trigger_rsi.shift(1) >= parameters.trigger_short_extreme) & (
        trigger_rsi <= parameters.trigger_short_recovery
    )
    raw_long = (
        (trend_rsi > parameters.trend_long_threshold)
        & (confirmation_rsi < parameters.confirmation_long_max)
        & long_recovery
    )
    raw_short = (
        (trend_rsi < parameters.trend_short_threshold)
        & (confirmation_rsi > parameters.confirmation_short_min)
        & short_recovery
    )

    result = pd.DataFrame(index=trigger_bars.index)
    result["trigger_rsi"] = trigger_rsi
    result["confirmation_rsi"] = confirmation_rsi
    result["trend_rsi"] = trend_rsi
    # 本根收盘才能确认RSI交叉，因此调用shift把执行严格推迟到下一根1分钟开盘。
    result["long_entry"] = raw_long.shift(1, fill_value=False).astype(bool)
    result["short_entry"] = raw_short.shift(1, fill_value=False).astype(bool)
    result["long_exit"] = raw_short.shift(1, fill_value=False).astype(bool)
    result["short_exit"] = raw_long.shift(1, fill_value=False).astype(bool)
    return result
