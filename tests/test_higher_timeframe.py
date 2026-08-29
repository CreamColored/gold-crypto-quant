"""高周期趋势确认的时间边界测试。"""

import numpy as np
import pandas as pd

from gold_crypto_quant.strategy.higher_timeframe import (
    build_ema_alignment_filter,
    build_higher_timeframe_filter,
)


def test_higher_timeframe_filter_waits_for_completed_bar() -> None:
    """1小时K线结束前，15分钟策略不能提前读取该小时收盘价。"""
    index = pd.date_range("2025-01-01", periods=12, freq="15min", tz="UTC")
    close = np.arange(100.0, 112.0)
    bars = pd.DataFrame({"close": close}, index=index)

    # 调用高周期确认方法并缩短EMA周期，使测试只聚焦高周期完成时间。
    result = build_higher_timeframe_filter(bars, "15m", ema_period=2)

    assert not result.loc[pd.Timestamp("2025-01-01 01:45:00Z"), "higher_long_allowed"]
    assert result.loc[pd.Timestamp("2025-01-01 02:00:00Z"), "higher_long_allowed"]


def test_ema12_alignment_waits_for_completed_confirmation_bar() -> None:
    """EMA12方向确认不得读取尚未收盘的30分钟K线。"""
    index = pd.date_range("2025-01-01", periods=20, freq="15min", tz="UTC")
    bars = pd.DataFrame({"close": np.arange(100.0, 120.0)}, index=index)

    # 调用EMA排列确认并缩短周期，使测试聚焦30分钟K线的可用时点。
    result = build_ema_alignment_filter(
        bars,
        "15m",
        fast_period=2,
        slow_period=3,
        trend_period=4,
    )

    assert not result.loc[pd.Timestamp("2025-01-01 01:45:00Z"), "higher_long_allowed"]
    assert result.loc[pd.Timestamp("2025-01-01 02:00:00Z"), "higher_long_allowed"]
