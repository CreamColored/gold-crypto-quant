"""多周期优先级和全局单持仓影子模拟测试。"""

import json
from pathlib import Path

import pandas as pd

from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    INTERVAL_PRIORITY,
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


def test_multi_symbol_uses_one_account_and_prefers_btc_on_same_interval(
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

    # BTC和ETH同周期同时触轨时只能开一笔；固定品种顺序选择BTC。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_symbol,
        state_path=state_path,
    )

    assert summary.position_side == "SHORT"
    assert summary.active_symbol == "BTC_USDT"
    assert summary.active_interval == "5m"
    assert sum("模拟开仓" in event.title for event in summary.events) == 1
    assert any("品种：BTC_USDT" in line for line in summary.events[0].lines)


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
    state.update(
        {
            "position_symbol": "BTC_USDT",
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
    eth_context = build_rotation_box_context(eth["15m"], parameters_for_same_timeframe("15m"))
    eth["15m"].iloc[-1, eth["15m"].columns.get_loc("high")] = (
        float(eth_context.iloc[-2]["bb_upper"]) + 0.5
    )

    # 30m止损已占用本批次；即使ETH 15m同时触轨，本轮也必须保持空仓观察。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_symbol,
        state_path=state_path,
    )

    assert summary.position_side == ""
    assert sum("模拟平仓" in event.title for event in summary.events) == 1
    assert not any("模拟开仓" in event.title for event in summary.events)


def test_target_does_not_reverse_when_current_bar_invalidates_box(tmp_path) -> None:
    bars_by_interval = _bars_by_interval()
    state_path = tmp_path / "broken-box.json"
    _initialize(bars_by_interval, state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "position_symbol": "ETH_USDT",
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
