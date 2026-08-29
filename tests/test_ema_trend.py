"""EMA 趋势信号和防未来数据行为测试。"""

import numpy as np
import pandas as pd

from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters, generate_ema_signals


def test_signal_executes_one_bar_after_confirmed_cross() -> None:
    # 构造先下跌后上涨的确定性价格，确保样本中只出现一次合格金叉。
    close = np.concatenate([np.linspace(200, 100, 220), np.linspace(100, 260, 100)])
    # 使用 UTC 的 15 分钟索引，模拟真实数据库读取出的标准化 K 线。
    index = pd.date_range("2026-01-01", periods=len(close), freq="15min", tz="UTC")
    bars = pd.DataFrame({"close": close}, index=index)
    # 测试使用较短周期缩短预热数据，但调用方式与正式 20/50/200 完全一致。
    signals = generate_ema_signals(
        bars, EmaTrendParameters(fast_period=5, slow_period=10, trend_period=20)
    )
    # 在测试中独立重算原始金叉，避免只验证函数自己输出的结果。
    raw_cross = (signals["ema_fast"] > signals["ema_slow"]) & (
        signals["ema_fast"].shift(1) <= signals["ema_slow"].shift(1)
    )
    qualified_cross = raw_cross & (signals["close"] > signals["ema_trend"])
    signal_times = signals.index[signals["long_entry"]]

    assert len(signal_times) == 1
    # flatnonzero 找到金叉所在位置，执行信号必须严格出现在它的下一根 K 线。
    cross_position = int(np.flatnonzero(qualified_cross.to_numpy())[0])
    assert signal_times[0] == signals.index[cross_position + 1]
    assert not signals.loc[signals.index[cross_position], "long_entry"]


def test_invalid_period_order_is_rejected() -> None:
    # 快线周期大于慢线没有策略含义，参数对象应在创建时立即拒绝。
    try:
        EmaTrendParameters(fast_period=50, slow_period=20, trend_period=200)
    except ValueError as exc:
        assert "fast < slow < trend" in str(exc)
    else:
        raise AssertionError("invalid EMA periods should fail")


def test_trend_slope_filter_blocks_entry_before_sustained_trend_turn() -> None:
    """价格刚反弹金叉时，EMA大趋势尚未持续转向，不应立即放行多头。"""
    close = np.concatenate([np.linspace(200, 100, 220), np.linspace(100, 260, 100)])
    index = pd.date_range("2026-01-01", periods=len(close), freq="15min", tz="UTC")
    bars = pd.DataFrame({"close": close}, index=index)

    # 先调用原始策略确认该反弹确实会产生一个多头执行信号。
    original = generate_ema_signals(
        bars,
        EmaTrendParameters(fast_period=5, slow_period=10, trend_period=20),
    )
    # 再调用EMA趋势斜率版本，同一时点的趋势线仍下降，因此必须屏蔽该次进场。
    slope_filtered = generate_ema_signals(
        bars,
        EmaTrendParameters(
            fast_period=5,
            slow_period=10,
            trend_period=20,
            trend_slope_lookback=10,
        ),
    )

    original_entry_time = original.index[original["long_entry"]][0]
    source_time = original.index[original.index.get_loc(original_entry_time) - 1]
    assert original.loc[original_entry_time, "long_entry"]
    assert original.loc[source_time, "ema_trend"] < original["ema_trend"].shift(10).loc[source_time]
    assert not slope_filtered.loc[original_entry_time, "long_entry"]
