"""从旧系统提取的EMA12和多周期RSI纯信号测试。"""

import numpy as np
import pandas as pd
import pytest

import gold_crypto_quant.strategy.multitimeframe_rsi as rsi_module
from gold_crypto_quant.strategy import (
    EmaTrendParameters,
    generate_ema_signals,
    generate_multitimeframe_rsi_signals,
)


def test_ema12_pullback_does_not_change_past_signals_from_future_price() -> None:
    """修改未来K线不得反向改变已经生成的EMA12回踩信号。"""
    index = pd.date_range("2025-01-01", periods=80, freq="15min", tz="UTC")
    close = np.linspace(100.0, 140.0, len(index))
    bars = pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
        },
        index=index,
    )
    parameters = EmaTrendParameters(
        fast_period=3,
        slow_period=5,
        trend_period=8,
        entry_mode="ema12_pullback",
    )

    # 先调用信号方法生成基准结果，再只篡改数据集最后一根尚属未来的价格。
    original = generate_ema_signals(bars, parameters)
    changed = bars.copy()
    changed.loc[index[-1], ["open", "high", "low", "close"]] = [50.0, 51.0, 49.0, 50.0]
    recalculated = generate_ema_signals(changed, parameters)

    pd.testing.assert_series_equal(
        original["long_entry"].iloc[:-1], recalculated["long_entry"].iloc[:-1]
    )
    pd.testing.assert_series_equal(
        original["short_entry"].iloc[:-1], recalculated["short_entry"].iloc[:-1]
    )


def test_multitimeframe_rsi_executes_one_trigger_bar_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1分钟RSI恢复交叉只能在下一根1分钟K线执行。"""
    trigger_index = pd.date_range("2025-01-02 02:00", periods=10, freq="1min", tz="UTC")
    confirmation_index = pd.date_range(
        "2025-01-02 00:00", periods=30, freq="5min", tz="UTC"
    )
    trend_index = pd.date_range("2025-01-01 00:00", periods=26, freq="1h", tz="UTC")
    trigger = pd.DataFrame({"close": np.arange(10.0)}, index=trigger_index)
    confirmation = pd.DataFrame({"close": np.arange(30.0)}, index=confirmation_index)
    trend = pd.DataFrame({"close": np.arange(26.0)}, index=trend_index)

    def controlled_rsi(close: pd.Series, _period: int) -> pd.Series:
        if len(close) == 10:
            values = [50.0] * 6 + [20.0, 35.0, 40.0, 45.0]
        elif len(close) == 30:
            values = [40.0] * len(close)
        else:
            values = [60.0] * len(close)
        return pd.Series(values, index=close.index)

    # 替换RSI数值生成，仅隔离验证多周期对齐和下一根执行边界。
    monkeypatch.setattr(rsi_module, "_rsi", controlled_rsi)
    signals = generate_multitimeframe_rsi_signals(trigger, confirmation, trend)

    assert not signals.loc[trigger_index[7], "long_entry"]
    assert signals.loc[trigger_index[8], "long_entry"]
