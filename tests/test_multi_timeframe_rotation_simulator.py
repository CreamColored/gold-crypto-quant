"""多周期优先级和全局单持仓影子模拟测试。"""

import json
from pathlib import Path

import pandas as pd
import pytest

from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    INTERVAL_PRIORITY,
    _middle_reduction_trigger,
    run_multi_timeframe_paper_cycle,
)
from gold_crypto_quant.strategy.bollinger_range import (
    build_rotation_box_context,
    parameters_for_same_timeframe,
)

FREQUENCIES = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}
LAST_OPEN = {
    "5m": "2026-01-01 11:55",
    "15m": "2026-01-01 11:45",
    "30m": "2026-01-01 11:30",
    "1h": "2026-01-01 11:00",
}


def test_wide_rotation_reduces_two_points_before_middle() -> None:
    assert _middle_reduction_trigger("ETH_USDT", "LONG", 80.0, 100.0, 1.0) == (
        98.0,
        2.0,
    )
    assert _middle_reduction_trigger("ETH_USDT", "SHORT", 120.0, 100.0, 1.0) == (
        102.0,
        2.0,
    )


def test_narrow_rotation_waits_for_exact_middle() -> None:
    assert _middle_reduction_trigger("ETH_USDT", "LONG", 95.0, 100.0, 1.0) == (
        100.0,
        0.0,
    )
    assert _middle_reduction_trigger("ETH_USDT", "SHORT", 105.0, 100.0, 1.0) == (
        100.0,
        0.0,
    )


def test_non_eth_reduces_at_eighty_percent_for_projected_100_percent_profit() -> None:
    long_trigger, long_advance = _middle_reduction_trigger("BTC_USDT", "LONG", 100.0, 101.0, 1.0)
    short_trigger, short_advance = _middle_reduction_trigger("BTC_USDT", "SHORT", 100.0, 99.0, 1.0)
    assert long_trigger == pytest.approx(100.8)
    assert long_advance == pytest.approx(0.2)
    assert short_trigger == pytest.approx(99.2)
    assert short_advance == pytest.approx(0.2)


def test_non_eth_waits_for_middle_below_projected_100_percent_profit() -> None:
    assert _middle_reduction_trigger("BTC_USDT", "LONG", 100.0, 100.5, 1.0) == (100.5, 0.0)


def _bars_by_interval() -> dict[str, pd.DataFrame]:
    """构造四个周期最后一根都在12:00完成的稳定宽箱体。"""
    result: dict[str, pd.DataFrame] = {}
    for interval in INTERVAL_PRIORITY:
        index = pd.date_range(
            end=pd.Timestamp(LAST_OPEN[interval], tz="UTC"),
            periods=41,
            freq=FREQUENCIES[interval],
        )
        closes = [97.0, 103.0] * 21
        closes = closes[:41]
        result[interval] = pd.DataFrame(
            {
                "open": closes,
                "high": [value + 1.0 for value in closes],
                "low": [value - 1.0 for value in closes],
                "close": closes,
                "volume": [10.0] * 41,
                "quote_volume": [1000.0] * 41,
            },
            index=index,
        )
    return result


def _initialize(bars_by_interval: dict[str, pd.DataFrame], state_path: Path) -> None:
    """调用前40根初始化四周期游标，不生成历史成交。"""
    run_multi_timeframe_paper_cycle(
        {interval: bars.iloc[:40] for interval, bars in bars_by_interval.items()},
        state_path=state_path,
    )


def test_multi_timeframe_prefers_5m_when_all_intervals_touch(tmp_path) -> None:
    bars_by_interval = _bars_by_interval()
    state_path = tmp_path / "multi.json"
    _initialize(bars_by_interval, state_path)
    for interval, bars in bars_by_interval.items():
        context = build_rotation_box_context(bars, parameters_for_same_timeframe(interval))
        bars.iloc[-1, bars.columns.get_loc("high")] = float(context.iloc[-2]["bb_upper"]) + 0.5

    # 四个周期同一时刻都有箱体且触上轨时，只允许最高优先级5m开空。
    summary = run_multi_timeframe_paper_cycle(bars_by_interval, state_path=state_path)

    assert summary.position_side == "SHORT"
    assert summary.active_interval == "5m"
    assert sum("模拟开仓" in event.title for event in summary.events) == 1


def test_multi_timeframe_falls_back_to_15m_when_5m_is_not_sideways(tmp_path) -> None:
    bars_by_interval = _bars_by_interval()
    state_path = tmp_path / "multi.json"
    # 让5m在触发前连续单边变化，上一根不再是有效震荡箱体。
    bars_5m = bars_by_interval["5m"]
    for offset, close in zip((-4, -3, -2), (100.0, 106.0, 112.0), strict=True):
        bars_5m.iloc[offset, bars_5m.columns.get_loc("open")] = close
        bars_5m.iloc[offset, bars_5m.columns.get_loc("close")] = close
        bars_5m.iloc[offset, bars_5m.columns.get_loc("high")] = close + 1.0
        bars_5m.iloc[offset, bars_5m.columns.get_loc("low")] = close - 1.0
    _initialize(bars_by_interval, state_path)
    context_15m = build_rotation_box_context(
        bars_by_interval["15m"], parameters_for_same_timeframe("15m")
    )
    bars_by_interval["15m"].iloc[-1, bars_by_interval["15m"].columns.get_loc("high")] = (
        float(context_15m.iloc[-2]["bb_upper"]) + 0.5
    )

    # 5m没有震荡资格时，应向下选择15m，仍只建立一笔空仓。
    summary = run_multi_timeframe_paper_cycle(bars_by_interval, state_path=state_path)

    assert summary.position_side == "SHORT"
    assert summary.active_interval == "15m"
    assert sum("模拟开仓" in event.title for event in summary.events) == 1


def test_multi_symbol_uses_one_account_and_allows_btc_eth_together(
    tmp_path,
) -> None:
    btc = _bars_by_interval()
    eth = {interval: bars.copy() for interval, bars in _bars_by_interval().items()}
    bars_by_symbol = {"BTC_USDT": btc, "ETH_USDT": eth}
    state_path = tmp_path / "multi-symbol.json"
    run_multi_timeframe_paper_cycle(
        {
            symbol: {interval: bars.iloc[:40] for interval, bars in symbol_bars.items()}
            for symbol, symbol_bars in bars_by_symbol.items()
        },
        state_path=state_path,
    )
    for symbol_bars in bars_by_symbol.values():
        context = build_rotation_box_context(symbol_bars["5m"], parameters_for_same_timeframe("5m"))
        symbol_bars["5m"].iloc[-1, symbol_bars["5m"].columns.get_loc("high")] = (
            float(context.iloc[-2]["bb_upper"]) + 0.5
        )

    # BTC和ETH同周期同时触轨时各开一笔，但仍共享同一份账户权益。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_symbol,
        state_path=state_path,
    )

    assert summary.position_side == "MULTIPLE"
    assert summary.active_symbol == "BTC_USDT,ETH_USDT"
    assert summary.active_interval == "5m,5m"
    assert sum("模拟开仓" in event.title for event in summary.events) == 2
    event_lines = [line for event in summary.events for line in event.lines]
    assert "品种：BTC_USDT" in event_lines
    assert "品种：ETH_USDT" in event_lines


def test_stop_does_not_fall_through_to_another_symbol_or_interval_in_same_batch(
    tmp_path,
) -> None:
    btc = _bars_by_interval()
    eth = {interval: bars.copy() for interval, bars in _bars_by_interval().items()}
    bars_by_symbol = {"BTC_USDT": btc, "ETH_USDT": eth}
    state_path = tmp_path / "single-batch.json"
    run_multi_timeframe_paper_cycle(
        {
            symbol: {interval: bars.iloc[:40] for interval, bars in symbol_bars.items()}
            for symbol, symbol_bars in bars_by_symbol.items()
        },
        state_path=state_path,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["positions"]["BTC_USDT"].update(
        {
            "position_side": "SHORT",
            "active_interval": "30m",
            "quantity": 1.0,
            "remaining_quantity": 1.0,
            "entry_price": 100.0,
            "stop_price": 105.0,
            "entry_fee_remaining": -0.01,
            "trade_net_pnl": 0.0,
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    btc["30m"].iloc[-1, btc["30m"].columns.get_loc("high")] = 106.0
    btc_context = build_rotation_box_context(btc["15m"], parameters_for_same_timeframe("15m"))
    btc["15m"].iloc[-1, btc["15m"].columns.get_loc("high")] = (
        float(btc_context.iloc[-2]["bb_upper"]) + 0.5
    )
    eth_context = build_rotation_box_context(eth["5m"], parameters_for_same_timeframe("5m"))
    eth["5m"].iloc[-1, eth["5m"].columns.get_loc("high")] = (
        float(eth_context.iloc[-2]["bb_upper"]) + 0.5
    )

    # BTC 30m止损后不能切到BTC 15m；ETH是独立品种，仍允许正常开仓。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_symbol,
        state_path=state_path,
    )

    assert summary.position_side == "SHORT"
    assert summary.active_symbol == "ETH_USDT"
    assert sum("模拟平仓" in event.title for event in summary.events) == 1
    open_events = [event for event in summary.events if "模拟开仓" in event.title]
    assert len(open_events) == 1
    assert "品种：ETH_USDT" in open_events[0].lines


def test_target_does_not_reverse_when_current_bar_invalidates_box(tmp_path) -> None:
    bars_by_interval = _bars_by_interval()
    state_path = tmp_path / "broken-box.json"
    _initialize(bars_by_interval, state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["positions"]["ETH_USDT"].update(
        {
            "position_side": "SHORT",
            "active_interval": "30m",
            "quantity": 1.0,
            "remaining_quantity": 1.0,
            "entry_price": 110.0,
            "stop_price": 120.0,
            "entry_fee_remaining": -0.01,
            "trade_net_pnl": 0.0,
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    bars_30m = bars_by_interval["30m"]
    context = build_rotation_box_context(bars_30m, parameters_for_same_timeframe("30m"))
    lower = float(context.iloc[-2]["bb_lower"])
    bars_30m.iloc[-1, bars_30m.columns.get_loc("low")] = lower - 10.0
    bars_30m.iloc[-1, bars_30m.columns.get_loc("close")] = lower - 9.0

    # 大阴线已破坏当前箱体时，只完成空单下轨止盈，不允许原地反手做多。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_interval,
        state_path=state_path,
    )

    assert summary.position_side == ""
    assert any("到达对侧轨止盈" in event.title for event in summary.events)
    assert not any("模拟开仓" in event.title for event in summary.events)
