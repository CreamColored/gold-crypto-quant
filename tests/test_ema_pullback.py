"""EMA趋势排列后的回踩恢复信号测试。"""

import numpy as np
import pandas as pd

from gold_crypto_quant.strategy import EmaTrendParameters, generate_ema_signals


def test_pullback_recovery_executes_on_next_bar() -> None:
    """回踩恢复在收盘确认后，只能在下一根K线产生执行信号。"""
    index = pd.date_range("2025-01-01", periods=100, freq="15min", tz="UTC")
    close = np.linspace(100.0, 150.0, len(index))
    close[-6:] = [146.0, 145.0, 145.0, 146.0, 148.0, 150.0]
    open_price = close - 0.5
    bars = pd.DataFrame(
        {
            "open": open_price,
            "high": np.maximum(open_price, close) + 0.5,
            "low": np.minimum(open_price, close) - 0.5,
            "close": close,
        },
        index=index,
    )
    parameters = EmaTrendParameters(
        fast_period=3,
        slow_period=8,
        trend_period=20,
        entry_mode="pullback",
        pullback_lookback=5,
    )

    # 调用信号生成方法，恢复发生在00:15，执行信号必须后移到00:30。
    signals = generate_ema_signals(bars, parameters)

    assert signals["long_entry"].sum() == 1
    assert signals.index[signals["long_entry"]][0] == pd.Timestamp("2025-01-02 00:30:00Z")
