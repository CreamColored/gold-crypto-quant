"""布林带震荡策略的多周期时序和信号测试。"""

from datetime import UTC, datetime

import pandas as pd

from gold_crypto_quant.strategy.bollinger_range import (
    BollingerRangeParameters,
    align_completed_regime_to_5m,
    build_rotation_box_context,
    evaluate_bollinger_next_bar_signal,
    evaluate_same_timeframe_next_bar_signal,
    parameters_for_same_timeframe,
)
from gold_crypto_quant.strategy.live_signals import SignalAction


def _bars(index: pd.DatetimeIndex, close: float = 100.0) -> pd.DataFrame:
    """构造满足指标列要求的连续测试K线。"""
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 10.0,
            "quote_volume": close * 10.0,
        },
        index=index,
    )


def test_15m_regime_is_visible_only_after_full_close() -> None:
    bars_5m = _bars(pd.date_range("2026-01-01", periods=120, freq="5min", tz="UTC"))
    bars_15m = _bars(pd.date_range("2026-01-01", periods=40, freq="15min", tz="UTC"))
    parameters = BollingerRangeParameters(maximum_band_drift=100.0, maximum_adx=100.0)

    # 调用高周期映射后，00:10结束的5分钟K线仍不能看到00:00的15分钟收盘结果。
    aligned = align_completed_regime_to_5m(bars_5m, bars_15m, parameters)

    assert pd.isna(aligned.loc[pd.Timestamp("2026-01-01 00:05", tz="UTC"), "source_15m_time"])
    assert aligned.loc[
        pd.Timestamp("2026-01-01 00:10", tz="UTC"), "source_15m_time"
    ] == pd.Timestamp("2026-01-01 00:00", tz="UTC")


def test_latest_touch_signal_executes_on_next_5m_open(monkeypatch) -> None:
    index = pd.date_range("2026-01-01", periods=100, freq="5min", tz="UTC")
    bars_5m = _bars(index)
    bars_15m = _bars(pd.date_range("2025-12-31", periods=100, freq="15min", tz="UTC"))
    fake_context = pd.DataFrame(
        {
            "long_entry": [False] * 99 + [True],
            "short_entry": [False] * 100,
            "sideways": [True] * 100,
            "regime_id": [1.0] * 100,
            "bb_upper": [102.0] * 100,
            "bb_middle": [100.0] * 100,
            "bb_lower": [98.0] * 100,
            "adx": [10.0] * 100,
            "volume_ratio": [1.0] * 100,
        },
        index=index,
    )
    monkeypatch.setattr(
        "gold_crypto_quant.strategy.bollinger_range.build_bollinger_entry_context",
        lambda *_args, **_kwargs: fake_context,
    )

    # 调用实时信号方法，确认收盘信号不会错误落在当前K线，而是计划下一根开盘执行。
    decision = evaluate_bollinger_next_bar_signal(bars_5m, bars_15m)

    assert decision.bar_time == datetime(2026, 1, 1, 8, 20, tzinfo=UTC)
    assert decision.actions == (SignalAction("LONG", "ENTRY"),)
    assert decision.payload["strategy"] == "BOLLINGER_RANGE"


def test_same_timeframe_signal_uses_its_own_next_bar(monkeypatch) -> None:
    index = pd.date_range("2026-01-01", periods=100, freq="15min", tz="UTC")
    bars = _bars(index)
    fake_context = pd.DataFrame(
        {
            "long_entry": [False] * 100,
            "short_entry": [False] * 99 + [True],
            "sideways": [True] * 100,
            "regime_id": [3.0] * 100,
            "bb_upper": [102.0] * 100,
            "bb_middle": [100.0] * 100,
            "bb_lower": [98.0] * 100,
            "adx": [10.0] * 100,
            "volume_ratio": [1.0] * 100,
        },
        index=index,
    )
    monkeypatch.setattr(
        "gold_crypto_quant.strategy.bollinger_range.build_same_timeframe_entry_context",
        lambda *_args, **_kwargs: fake_context,
    )

    # 调用15分钟同周期方法，确认不会再落到5分钟执行时间。
    decision = evaluate_same_timeframe_next_bar_signal(bars, "15m")

    assert decision.bar_time == index[-1].to_pydatetime() + pd.Timedelta(minutes=15)
    assert decision.actions == (SignalAction("SHORT", "ENTRY"),)
    assert decision.payload["signal_interval"] == "15m"


def test_same_timeframe_drift_thresholds_are_interval_specific() -> None:
    # 调用周期参数工厂，确认朋友定义的15分钟2.5点与5分钟1点没有混用。
    assert parameters_for_same_timeframe("15m").maximum_band_drift == 2.5
    assert parameters_for_same_timeframe("5m").maximum_band_drift == 1.0


def test_rotation_box_uses_continuous_three_bar_drift() -> None:
    index = pd.date_range("2026-01-01", periods=40, freq="15min", tz="UTC")
    closes = [97.0, 103.0] * 20
    bars = _bars(index)
    bars["close"] = closes
    bars["open"] = closes
    bars["high"] = [value + 1.0 for value in closes]
    bars["low"] = [value - 1.0 for value in closes]

    # 调用轨道轮转箱体计算，稳定重复波动应形成可交易箱体候选。
    context = build_rotation_box_context(bars)

    assert bool(context.iloc[-1]["box_candidate"]) is True
    assert bool(context.iloc[-1]["breakout"]) is False
