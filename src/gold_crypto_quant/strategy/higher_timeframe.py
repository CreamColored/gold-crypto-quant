"""使用已完成高周期K线确认低周期EMA交易方向。"""

import pandas as pd

HIGHER_TIMEFRAME_RULES = {
    "5m": ("1h", 12),
    "15m": ("1h", 4),
    "30m": ("1h", 2),
    "1h": ("4h", 4),
}

EMA12_CONFIRMATION_RULES = {
    "15m": ("30min", 2),
    "30m": ("1h", 2),
}


def build_higher_timeframe_filter(
    bars: pd.DataFrame,
    interval: str,
    *,
    ema_period: int = 200,
) -> pd.DataFrame:
    """返回与低周期索引对齐的高周期多空许可布尔列。"""
    if interval not in HIGHER_TIMEFRAME_RULES:
        raise ValueError(f"unsupported higher-timeframe interval: {interval}")
    if ema_period < 2:
        raise ValueError("ema_period must be at least 2")
    if "close" not in bars.columns:
        raise ValueError("bars must contain a 'close' column")

    higher_frequency, expected_count = HIGHER_TIMEFRAME_RULES[interval]
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    # 开盘时间属于左闭右开区间；标签放在右端，只有高周期完整结束后才能被低周期读取。
    resampler = close.resample(higher_frequency, closed="left", label="right")
    completed_close = resampler.last()
    completed_count = resampler.count()
    # 数据集开头或断档造成的不完整高周期K线不能参与方向确认。
    completed_close = completed_close.where(completed_count == expected_count)
    higher_ema = completed_close.ewm(
        span=ema_period,
        adjust=False,
        min_periods=ema_period,
    ).mean()

    higher_frame = pd.DataFrame(
        {
            "higher_close": completed_close,
            "higher_ema": higher_ema,
            "higher_long_allowed": completed_close > higher_ema,
            "higher_short_allowed": completed_close < higher_ema,
        }
    )
    # reindex(..., method="ffill")只传播标签时间已经到达的高周期结果，不会读取未完成区间。
    aligned = higher_frame.reindex(bars.index, method="ffill")
    aligned[["higher_long_allowed", "higher_short_allowed"]] = aligned[
        ["higher_long_allowed", "higher_short_allowed"]
    ].fillna(False)
    return aligned


def build_ema_alignment_filter(
    bars: pd.DataFrame,
    interval: str,
    *,
    fast_period: int = 12,
    slow_period: int = 144,
    trend_period: int = 169,
) -> pd.DataFrame:
    """用已完整收盘的上一层周期确认EMA12方向，专供研究模块使用。"""
    if interval not in EMA12_CONFIRMATION_RULES:
        raise ValueError("EMA12 pullback research supports only 15m and 30m")
    if not 1 < fast_period < slow_period < trend_period:
        raise ValueError("EMA alignment periods must satisfy 1 < fast < slow < trend")
    if "close" not in bars.columns:
        raise ValueError("bars must contain a 'close' column")

    higher_frequency, expected_count = EMA12_CONFIRMATION_RULES[interval]
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    # 调用重采样器把低周期收盘价合成为30分钟或1小时K线，标签放在完成时刻。
    resampler = close.resample(higher_frequency, closed="left", label="right")
    completed_close = resampler.last().where(resampler.count() == expected_count)
    higher_fast = completed_close.ewm(
        span=fast_period, adjust=False, min_periods=fast_period
    ).mean()
    higher_slow = completed_close.ewm(
        span=slow_period, adjust=False, min_periods=slow_period
    ).mean()
    higher_trend = completed_close.ewm(
        span=trend_period, adjust=False, min_periods=trend_period
    ).mean()
    higher_frame = pd.DataFrame(
        {
            "higher_long_allowed": (higher_fast > higher_slow)
            & (higher_fast > higher_trend),
            "higher_short_allowed": (higher_fast < higher_slow)
            & (higher_fast < higher_trend),
        }
    )
    # 只有时间戳已经到达的完整高周期结果才能向前填充，不读取仍在形成的高周期K线。
    aligned = higher_frame.reindex(bars.index, method="ffill").fillna(False)
    return aligned.astype(bool)
