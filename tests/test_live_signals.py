"""下一根K线EMA信号时序和去重身份测试。"""

from datetime import UTC, datetime

import pandas as pd

from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters
from gold_crypto_quant.strategy.live_signals import (
    SignalAction,
    build_signal_dedupe_key,
    evaluate_next_bar_signal,
)


def _cross_up_bars() -> pd.DataFrame:
    """构造最后一根才发生向上交叉的连续已收盘K线。"""
    index = pd.date_range("2026-01-01", periods=201, freq="5min", tz="UTC")
    close = [100.0] * 200 + [110.0]
    return pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": [1.0] * 201,
            "quote_volume": [100.0] * 201,
        },
        index=index,
    )


def test_latest_closed_cross_becomes_next_bar_open_signal() -> None:
    bars = _cross_up_bars()

    # 调用实时信号适配层，最新收盘确认的金叉应落在下一根5分钟K线开盘。
    decision = evaluate_next_bar_signal(bars, "5m")

    assert decision.bar_time == bars.index[-1].to_pydatetime() + pd.Timedelta(minutes=5)
    assert decision.signal_price == 110
    assert SignalAction("LONG", "ENTRY") in decision.actions
    assert SignalAction("SHORT", "EXIT") in decision.actions
    assert decision.payload["source_bar_open_time"] == bars.index[-1].isoformat()


def test_signal_dedupe_key_is_stable_and_action_specific() -> None:
    values = {
        "strategy_name": "EMA_TREND",
        "strategy_version": "1.0.0",
        "instrument_id": 7,
        "interval": "5m",
        "bar_time": datetime(2026, 1, 1, tzinfo=UTC),
    }

    # 相同动作重跑键必须一致，不同动作必须生成不同键。
    first = build_signal_dedupe_key(**values, action=SignalAction("LONG", "ENTRY"))
    retry = build_signal_dedupe_key(**values, action=SignalAction("LONG", "ENTRY"))
    exit_key = build_signal_dedupe_key(**values, action=SignalAction("LONG", "EXIT"))

    assert first == retry
    assert first != exit_key
    assert len(first) == 64


def test_live_direction_filter_matches_backtest_direction_rule() -> None:
    bars = _cross_up_bars()

    # 仅空参数必须过滤金叉产生的多头开仓，但仍保留用于平空的退出动作。
    decision = evaluate_next_bar_signal(
        bars,
        "5m",
        parameters=EmaTrendParameters(allow_long=False, allow_short=True),
    )

    assert SignalAction("LONG", "ENTRY") not in decision.actions
    assert SignalAction("SHORT", "EXIT") in decision.actions
