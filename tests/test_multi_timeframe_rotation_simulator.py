"""多周期优先级和全局单持仓影子模拟测试。"""

import json
from pathlib import Path

import pandas as pd
import pytest

from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    FIXED_STOP_DISTANCE,
    INTERVAL_PRIORITY,
    _bands_are_opening,
    _block_symbol_after_stop,
    _has_bottom_structure,
    _has_top_structure,
    _ladder_step_points,
    _micro_zones_allow_entry,
    _middle_reduction_trigger,
    _new_state,
    _parameters_for_symbol,
    _refresh_box_qualification,
    _try_release_symbol_after_wait,
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


def test_symbol_specific_stop_distances_follow_trading_rules() -> None:
    bars = pd.DataFrame(
        {"close": [80_000.0] * 30},
        index=pd.date_range("2026-01-01", periods=30, freq="5min", tz="UTC"),
    )
    for symbol, intervals in FIXED_STOP_DISTANCE.items():
        for interval, expected in intervals.items():
            assert _parameters_for_symbol(symbol, interval, bars).fixed_stop_distance == expected


def test_micro_filter_requires_lower_confluence_for_long_entries() -> None:
    assert _micro_zones_allow_entry("ETH_USDT", "LONG", 0.1, -0.2)[0] is True
    assert _micro_zones_allow_entry("ETH_USDT", "LONG", 0.1, 0.5)[0] is False


def test_micro_filter_blocks_eth_short_when_both_periods_are_above_upper_band() -> None:
    assert _micro_zones_allow_entry("ETH_USDT", "SHORT", 1.1, 1.2)[0] is False
    assert _micro_zones_allow_entry("ETH_USDT", "SHORT", 1.1, 0.9)[0] is True
    assert _micro_zones_allow_entry("BTC_USDT", "SHORT", 1.1, 1.2)[0] is True


def test_micro_filter_rejects_middle_zone_for_short_entries() -> None:
    assert _micro_zones_allow_entry("BTC_USDT", "SHORT", 0.6, 1.1)[0] is False


def test_invalid_latest_box_cannot_reuse_stale_qualification() -> None:
    state = _new_state(10_000.0, ("ETH_USDT",))
    state.box_active["ETH_USDT"]["15m"] = True

    _refresh_box_qualification(
        state,
        "ETH_USDT",
        "15m",
        box_candidate=False,
        breakout=False,
    )

    assert state.box_active["ETH_USDT"]["15m"] is False


def test_stop_blocks_whole_symbol_until_a_complete_new_box() -> None:
    state = _new_state(10_000.0, ("ETH_USDT",))
    for interval in INTERVAL_PRIORITY:
        state.box_active["ETH_USDT"][interval] = True
    stop_time = pd.Timestamp("2026-01-01 12:00", tz="UTC")

    _block_symbol_after_stop(state, "ETH_USDT", "15m", stop_time)

    assert state.symbol_blocked_after_stop["ETH_USDT"] is True
    assert all(state.blocked_after_stop["ETH_USDT"].values())
    assert not any(state.box_active["ETH_USDT"].values())
    assert state.symbol_stopped_interval["ETH_USDT"] == "15m"
    assert pd.Timestamp(state.symbol_resume_check_after["ETH_USDT"]) == stop_time + pd.Timedelta(
        minutes=30
    )


def test_wait_uses_closed_bar_and_keeps_waiting_while_bands_open() -> None:
    state = _new_state(10_000.0, ("ETH_USDT",))
    stop_time = pd.Timestamp("2026-01-01 12:00", tz="UTC")
    _block_symbol_after_stop(state, "ETH_USDT", "15m", stop_time)
    index = pd.date_range(stop_time, periods=3, freq="15min")
    opening = pd.DataFrame(
        {
            "bb_upper": [110.0, 111.0, 112.0],
            "bb_lower": [90.0, 89.0, 88.0],
            "bb_width": [20.0, 22.0, 24.0],
            "box_candidate": [True, True, True],
            "breakout": [False, False, False],
        },
        index=index,
    )
    contexts = {"ETH_USDT": {"15m": opening}}
    batch = [("ETH_USDT", "15m", 2)]

    assert _bands_are_opening(opening, 2) is True
    assert (
        _try_release_symbol_after_wait(
            state,
            "ETH_USDT",
            stop_time + pd.Timedelta(minutes=30),
            batch,
            contexts,
        )
        is False
    )
    assert state.symbol_blocked_after_stop["ETH_USDT"] is True

    flat = opening.copy()
    flat.loc[index[-1], ["bb_upper", "bb_lower", "bb_width"]] = [111.0, 89.0, 22.0]
    contexts["ETH_USDT"]["15m"] = flat

    assert (
        _try_release_symbol_after_wait(
            state,
            "ETH_USDT",
            stop_time + pd.Timedelta(minutes=30),
            batch,
            contexts,
        )
        is True
    )

    assert state.symbol_blocked_after_stop["ETH_USDT"] is False
    assert state.box_active["ETH_USDT"]["15m"] is True
    assert not any(state.blocked_after_stop["ETH_USDT"].values())


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
    """构造三个交易周期最后一根都在12:00完成的稳定宽箱体。"""
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


MICRO_END = "2026-01-01 12:01"


def _micro_bars(price: float = 100.0, periods: int = 70) -> pd.DataFrame:
    """构造平坦的1分钟K线；最后一根留给测试改写成触轨。"""
    index = pd.date_range(end=pd.Timestamp(MICRO_END, tz="UTC"), periods=periods, freq="1min")
    return pd.DataFrame(
        {
            "open": [price] * periods,
            "high": [price + 0.5] * periods,
            "low": [price - 0.5] * periods,
            "close": [price] * periods,
            "volume": [10.0] * periods,
            "quote_volume": [1000.0] * periods,
        },
        index=index,
    )


def _touch_band(micro: pd.DataFrame, level: float, *, upper: bool) -> None:
    """把最后一根1分钟K线改写成刚好触及指定轨道，另一侧保持不触及。"""
    row = len(micro) - 1
    if upper:
        micro.iloc[row, micro.columns.get_loc("high")] = level + 0.4
        micro.iloc[row, micro.columns.get_loc("low")] = level - 0.2
        micro.iloc[row, micro.columns.get_loc("open")] = level - 0.1
        micro.iloc[row, micro.columns.get_loc("close")] = level - 0.1
    else:
        micro.iloc[row, micro.columns.get_loc("low")] = level - 0.4
        micro.iloc[row, micro.columns.get_loc("high")] = level + 0.2
        micro.iloc[row, micro.columns.get_loc("open")] = level + 0.1
        micro.iloc[row, micro.columns.get_loc("close")] = level + 0.1


def _activate_boxes(state_path: Path, symbols: tuple[str, ...], intervals: tuple[str, ...]) -> None:
    """直接置位箱体资格，跳过多轮收线确认，让测试聚焦在触轨行为本身。"""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for symbol in symbols:
        for interval in intervals:
            state["box_active"][symbol][interval] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _initialize(
    bars_by_interval: dict[str, pd.DataFrame],
    state_path: Path,
    micro: pd.DataFrame | None = None,
    symbols: tuple[str, ...] = ("ETH_USDT",),
) -> None:
    """建立四周期与逐分钟游标，不生成历史成交。

    传入1分钟行情时用去掉最后一根的切片，让测试改写的那根成为“新增”K线触发触轨。
    """
    kwargs = {}
    if micro is not None:
        kwargs["micro_bars_by_symbol"] = dict.fromkeys(symbols, micro.iloc[:-1])
    run_multi_timeframe_paper_cycle(
        {interval: bars.iloc[:40] for interval, bars in bars_by_interval.items()},
        state_path=state_path,
        **kwargs,
    )


def _invalidate_sideways_before_last_bar(bars: pd.DataFrame) -> None:
    """让上一根附近的三轨明显移动，供周期优先级降级测试使用。"""
    for offset, close in zip((-4, -3, -2), (100.0, 106.0, 112.0), strict=True):
        bars.iloc[offset, bars.columns.get_loc("open")] = close
        bars.iloc[offset, bars.columns.get_loc("close")] = close
        bars.iloc[offset, bars.columns.get_loc("high")] = close + 1.0
        bars.iloc[offset, bars.columns.get_loc("low")] = close - 1.0


def test_multi_timeframe_prefers_5m_when_all_intervals_touch(tmp_path) -> None:
    bars_by_interval = _bars_by_interval()
    micro = _micro_bars()
    state_path = tmp_path / "multi.json"
    _initialize(bars_by_interval, state_path, micro)
    _activate_boxes(state_path, ("ETH_USDT",), INTERVAL_PRIORITY)
    context = build_rotation_box_context(
        bars_by_interval["5m"], parameters_for_same_timeframe("5m")
    )
    _touch_band(micro, float(context.iloc[-1]["bb_upper"]), upper=True)

    # 四个周期同一时刻都有箱体且触上轨时，只允许最高优先级5m开空。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_interval,
        micro_bars_by_symbol={"ETH_USDT": micro},
        state_path=state_path,
    )

    assert summary.position_side == "SHORT"
    assert summary.active_interval == "5m"
    assert sum("模拟开仓" in event.title for event in summary.events) == 1


def test_multi_timeframe_falls_back_to_30m_when_15m_is_not_sideways(tmp_path) -> None:
    bars_by_interval = _bars_by_interval()
    state_path = tmp_path / "multi.json"
    # 让5m和15m都失去震荡资格，才允许按优先级降级到30m。
    _invalidate_sideways_before_last_bar(bars_by_interval["5m"])
    _invalidate_sideways_before_last_bar(bars_by_interval["15m"])
    micro = _micro_bars()
    _initialize(bars_by_interval, state_path, micro)
    # 只放行30m与1h的箱体资格，模拟5m/15m已失去震荡。
    _activate_boxes(state_path, ("ETH_USDT",), ("30m", "1h"))
    context_30m = build_rotation_box_context(
        bars_by_interval["30m"], parameters_for_same_timeframe("30m")
    )
    _touch_band(micro, float(context_30m.iloc[-1]["bb_upper"]), upper=True)

    # 15m没有震荡资格时，应向下选择30m，仍只建立一笔空仓。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_interval,
        micro_bars_by_symbol={"ETH_USDT": micro},
        state_path=state_path,
    )

    assert summary.position_side == "SHORT"
    assert summary.active_interval == "30m"
    assert sum("模拟开仓" in event.title for event in summary.events) == 1


def test_confirmed_box_opens_on_new_one_minute_touch_without_waiting_main_close(
    tmp_path,
) -> None:
    """15分钟没有新增收线时，新增1分钟K线触轨也必须立即产生开仓。"""
    bars_by_interval = _bars_by_interval()
    state_path = tmp_path / "intrabar-touch.json"
    micro_index = pd.date_range(
        end=pd.Timestamp("2026-01-01 12:01", tz="UTC"),
        periods=66,
        freq="1min",
    )
    micro = pd.DataFrame(
        {
            "open": [100.0] * 66,
            "high": [100.5] * 66,
            "low": [99.5] * 66,
            "close": [100.0] * 66,
            "volume": [10.0] * 66,
            "quote_volume": [1000.0] * 66,
        },
        index=micro_index,
    )
    # 首次调用只初始化到12:00，禁止历史触轨回放。
    run_multi_timeframe_paper_cycle(
        bars_by_interval,
        micro_bars_by_symbol={"ETH_USDT": micro.iloc[:-1]},
        state_path=state_path,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["box_active"]["ETH_USDT"]["15m"] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")

    context = build_rotation_box_context(
        bars_by_interval["15m"],
        parameters_for_same_timeframe("15m"),
    )
    lower = float(context.iloc[-1]["bb_lower"])
    micro.iloc[-1, micro.columns.get_loc("low")] = lower - 0.1
    micro.iloc[-1, micro.columns.get_loc("open")] = lower + 0.2
    micro.iloc[-1, micro.columns.get_loc("close")] = lower + 0.2
    micro.iloc[-1, micro.columns.get_loc("high")] = lower + 0.4
    summary = run_multi_timeframe_paper_cycle(
        bars_by_interval,
        micro_bars_by_symbol={"ETH_USDT": micro},
        state_path=state_path,
    )

    assert summary.processed_bars == 0
    assert summary.position_side == "LONG"
    assert summary.active_interval == "15m"
    open_events = [event for event in summary.events if "模拟开仓" in event.title]
    assert len(open_events) == 1
    assert open_events[0].event_key.startswith("rotation-v5:2026-01-01T12:01:00+00:00")
    assert any("触及下轨即时开仓" in line for line in open_events[0].lines)

    # 同一份1分钟行情再次轮询时，游标必须阻止重复开仓和重复邮件。
    repeated = run_multi_timeframe_paper_cycle(
        bars_by_interval,
        micro_bars_by_symbol={"ETH_USDT": micro},
        state_path=state_path,
    )
    assert not repeated.events


def test_multi_symbol_uses_one_account_and_allows_btc_eth_together(
    tmp_path,
) -> None:
    btc = _bars_by_interval()
    eth = {interval: bars.copy() for interval, bars in _bars_by_interval().items()}
    _invalidate_sideways_before_last_bar(btc["5m"])
    _invalidate_sideways_before_last_bar(eth["5m"])
    bars_by_symbol = {"BTC_USDT": btc, "ETH_USDT": eth}
    micro = {"BTC_USDT": _micro_bars(), "ETH_USDT": _micro_bars()}
    state_path = tmp_path / "multi-symbol.json"
    run_multi_timeframe_paper_cycle(
        {
            symbol: {interval: bars.iloc[:40] for interval, bars in symbol_bars.items()}
            for symbol, symbol_bars in bars_by_symbol.items()
        },
        micro_bars_by_symbol={symbol: bars.iloc[:-1] for symbol, bars in micro.items()},
        state_path=state_path,
    )
    # 5m已被打散，只放行15m及以上，让两个品种都落在15m开仓。
    _activate_boxes(state_path, ("BTC_USDT", "ETH_USDT"), ("15m", "30m", "1h"))
    for symbol, symbol_bars in bars_by_symbol.items():
        context = build_rotation_box_context(
            symbol_bars["15m"], parameters_for_same_timeframe("15m")
        )
        _touch_band(micro[symbol], float(context.iloc[-1]["bb_upper"]), upper=True)

    # BTC和ETH同周期同时触轨时各开一笔，但仍共享同一份账户权益。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_symbol,
        micro_bars_by_symbol=micro,
        state_path=state_path,
    )

    assert summary.position_side == "MULTIPLE"
    assert summary.active_symbol == "BTC_USDT,ETH_USDT"
    assert summary.active_interval == "15m,15m"
    assert sum("模拟开仓" in event.title for event in summary.events) == 2
    event_lines = [line for event in summary.events for line in event.lines]
    assert "品种：BTC_USDT" in event_lines
    assert "品种：ETH_USDT" in event_lines


def test_adding_a_new_symbol_to_an_existing_state_file_does_not_crash(tmp_path) -> None:
    """老状态文件里没见过的新品种（比如后接入的XAU_USDT）必须原地补齐，不能KeyError，
    也不能重置已有品种积累的权益和历史。"""
    state_path = tmp_path / "add-new-symbol.json"
    btc = _bars_by_interval()
    run_multi_timeframe_paper_cycle({"BTC_USDT": btc}, state_path=state_path)
    equity_before = json.loads(state_path.read_text(encoding="utf-8"))["equity"]

    xau = _bars_by_interval()
    summary = run_multi_timeframe_paper_cycle(
        {"BTC_USDT": btc, "XAU_USDT": xau},
        state_path=state_path,
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["equity"] == equity_before
    assert "XAU_USDT" in payload["positions"]
    assert "XAU_USDT" in payload["box_active"]
    assert "XAU_USDT" in payload["last_bar_times"]
    assert summary.status != "ERROR"


def test_stop_does_not_fall_through_to_another_symbol_or_interval_in_same_batch(
    tmp_path,
) -> None:
    btc = _bars_by_interval()
    eth = {interval: bars.copy() for interval, bars in _bars_by_interval().items()}
    _invalidate_sideways_before_last_bar(eth["5m"])
    bars_by_symbol = {"BTC_USDT": btc, "ETH_USDT": eth}
    micro = {"BTC_USDT": _micro_bars(), "ETH_USDT": _micro_bars()}
    state_path = tmp_path / "single-batch.json"
    run_multi_timeframe_paper_cycle(
        {
            symbol: {interval: bars.iloc[:40] for interval, bars in symbol_bars.items()}
            for symbol, symbol_bars in bars_by_symbol.items()
        },
        micro_bars_by_symbol={symbol: bars.iloc[:-1] for symbol, bars in micro.items()},
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
    for symbol in ("BTC_USDT", "ETH_USDT"):
        for interval in ("15m", "30m", "1h"):
            state["box_active"][symbol][interval] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")
    # BTC这一分钟冲上105以上触发持仓止损。
    _touch_band(micro["BTC_USDT"], 106.0, upper=True)
    eth_context = build_rotation_box_context(eth["15m"], parameters_for_same_timeframe("15m"))
    _touch_band(micro["ETH_USDT"], float(eth_context.iloc[-1]["bb_upper"]), upper=True)

    # BTC 30m止损后不能切到BTC 15m；ETH是独立品种，仍允许正常开仓。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_symbol,
        micro_bars_by_symbol=micro,
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
    micro = _micro_bars()
    state_path = tmp_path / "broken-box.json"
    _initialize(bars_by_interval, state_path, micro)
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
    # 大阴线打穿30m箱体：先改写主周期K线，再按改写后重算的轨道设置分钟触及价，
    # 否则策略运行时算出的下轨与测试使用的不是同一个值。
    bars_30m = bars_by_interval["30m"]
    first_pass = build_rotation_box_context(bars_30m, parameters_for_same_timeframe("30m"))
    rough_lower = float(first_pass.iloc[-1]["bb_lower"])
    bars_30m.iloc[-1, bars_30m.columns.get_loc("low")] = rough_lower - 10.0
    bars_30m.iloc[-1, bars_30m.columns.get_loc("close")] = rough_lower - 9.0
    context = build_rotation_box_context(bars_30m, parameters_for_same_timeframe("30m"))
    _touch_band(micro, float(context.iloc[-1]["bb_lower"]), upper=False)

    # 大阴线已破坏当前箱体时，只完成空单下轨止盈，不允许原地反手做多。
    summary = run_multi_timeframe_paper_cycle(
        bars_by_interval,
        micro_bars_by_symbol={"ETH_USDT": micro},
        state_path=state_path,
    )

    assert summary.position_side == ""
    assert any("到达对侧轨止盈" in event.title for event in summary.events)
    assert not any("模拟开仓" in event.title for event in summary.events)


def _divergence_bars(prices: list[float]) -> pd.DataFrame:
    """把一串收盘价补成MACD可用的K线，足够长以让EMA26收敛。"""
    warmup = [prices[0]] * 60
    closes = warmup + prices
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "close": closes,
            "volume": [10.0] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="5min", tz="UTC"),
    )


def _rally(start: float, end: float, steps: int) -> list[float]:
    span = (end - start) / (steps - 1)
    return [start + span * i for i in range(steps)]


def test_top_structure_detects_macd_bearish_divergence() -> None:
    """价格创新高但DIF更低＝顶背离；第二个高点靠更慢的爬升制造动能衰减。"""
    prices = (
        _rally(100.0, 118.0, 10)  # 第一波急涨，DIF冲高
        + _rally(118.0, 104.0, 8)  # 回调
        + _rally(104.0, 120.0, 22)  # 第二波缓慢爬升到更高价，但动能更弱
        + [118.0, 117.0]  # 右侧确认摆动点
    )

    assert _has_top_structure(_divergence_bars(prices)) is True


def test_top_structure_rejects_healthy_uptrend() -> None:
    """价格创新高且动能同步走强时不是顶背离。"""
    prices = (
        _rally(100.0, 110.0, 10)
        + _rally(110.0, 106.0, 5)
        + _rally(106.0, 140.0, 20)
        + [138.0, 137.0]
    )

    assert _has_top_structure(_divergence_bars(prices)) is False


def test_bottom_structure_detects_macd_bullish_divergence() -> None:
    """价格创新低但DIF更高＝底背离。"""
    prices = (
        _rally(120.0, 102.0, 10)
        + _rally(102.0, 116.0, 8)
        + _rally(116.0, 100.0, 22)
        + [102.0, 103.0]
    )

    assert _has_bottom_structure(_divergence_bars(prices)) is True


def test_structure_needs_enough_history_for_macd() -> None:
    """历史不足以让EMA26收敛时保持保守，不给出结构信号。"""
    short = pd.DataFrame(
        {
            "open": [100.0] * 10,
            "high": [101.0] * 10,
            "low": [99.0] * 10,
            "close": [100.0] * 10,
            "volume": [10.0] * 10,
        },
        index=pd.date_range("2026-01-01", periods=10, freq="5min", tz="UTC"),
    )

    assert _has_top_structure(short) is False
    assert _has_bottom_structure(short) is False


def test_ladder_step_scales_with_symbol_price() -> None:
    """8点是按ETH价位定的，BTC等高价品种必须等比放大，否则只相当于噪音。"""
    eth_step = _ladder_step_points(pd.DataFrame({"close": [2420.0] * 20}))
    btc_step = _ladder_step_points(pd.DataFrame({"close": [77400.0] * 20}))

    assert eth_step == pytest.approx(8.0)
    # 缩放后各品种步长占价格比例应当一致，都在0.32%附近。
    assert btc_step / 77400.0 == pytest.approx(eth_step / 2500.0, rel=1e-6)
    assert btc_step > 200.0
