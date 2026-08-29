"""EMA趋势突破入场的时间边界测试。"""

import numpy as np
import pandas as pd

from gold_crypto_quant.strategy import EmaTrendParameters, generate_ema_signals


def test_breakout_uses_previous_range_and_enters_next_bar() -> None:
    """突破本根不能参与历史区间，也不能在自身收盘前成交。"""
    index = pd.date_range("2025-01-01", periods=24, freq="15min", tz="UTC")
    close = np.linspace(100.0, 106.0, len(index))
    close[18] = 112.0
    close[19:] = np.linspace(111.0, 113.0, len(index) - 19)
    bars = pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        },
        index=index,
    )
    parameters = EmaTrendParameters(
        fast_period=3,
        slow_period=5,
        trend_period=8,
        entry_mode="breakout",
        pullback_lookback=5,
    )

    # 调用策略方法；第18根收盘确认突破，只有第19根开盘位置能够出现执行信号。
    signals = generate_ema_signals(bars, parameters)

    assert not signals.loc[index[18], "long_entry"]
    assert signals.loc[index[19], "long_entry"]


def test_breakout_future_price_cannot_rewrite_past_signal() -> None:
    """修改最后一根未来价格不得改变此前已经确认的突破信号。"""
    index = pd.date_range("2025-01-01", periods=40, freq="30min", tz="UTC")
    close = np.linspace(100.0, 120.0, len(index))
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
        },
        index=index,
    )
    parameters = EmaTrendParameters(
        fast_period=3,
        slow_period=5,
        trend_period=8,
        entry_mode="breakout",
        pullback_lookback=5,
    )

    # 分别调用两次策略方法，第二次只改变最后一根K线。
    original = generate_ema_signals(bars, parameters)
    changed = bars.copy()
    changed.loc[index[-1], ["open", "high", "low", "close"]] = [10.0, 11.0, 9.0, 10.0]
    recalculated = generate_ema_signals(changed, parameters)

    pd.testing.assert_series_equal(
        original["long_entry"].iloc[:-1], recalculated["long_entry"].iloc[:-1]
    )
    pd.testing.assert_series_equal(
        original["short_entry"].iloc[:-1], recalculated["short_entry"].iloc[:-1]
    )
