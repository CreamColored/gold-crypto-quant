"""在途K线进入触轨判定（方案B）的行为锁定。

三条最要紧的不变量：

1. 在途K线绝不推进游标。推过去之后，这一分钟收线时带着真实最高最低价再来，就会
   因为"不比游标新"而被整根跳过——用几秒钟的残缺数据做了决策，整分钟的真实极值
   被静默丢弃。
2. 触轨要连续成立满若干秒才开仓。BTC 每分钟的最高点有两成只停留了一秒，这些插针
   的证据强度远不如站住的触碰。
3. 开仓所在那一根收线后带着完整极值回来时，其中一部分发生在开仓之前——不能用
   入场前的价格把这一仓止损掉。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    MultiTimeframePaperState,
    _provisional_dwell_ready,
    run_multi_timeframe_paper_cycle,
)
from gold_crypto_quant.strategy.bollinger_range import (
    build_rotation_box_context,
    parameters_for_same_timeframe,
)
from tests.test_multi_timeframe_rotation_simulator import _bars_by_interval

NOW = datetime(2026, 1, 1, 12, 1, 30, tzinfo=UTC)


def _state() -> MultiTimeframePaperState:
    return MultiTimeframePaperState()


def test_dwell_requires_sustained_touch() -> None:
    """条件刚成立不开仓；连续成立满时长才放行。"""
    state = _state()
    assert not _provisional_dwell_ready(state, "ETH_USDT", True, NOW, 3.0)
    assert not _provisional_dwell_ready(
        state, "ETH_USDT", True, NOW + timedelta(seconds=2), 3.0
    )
    assert _provisional_dwell_ready(state, "ETH_USDT", True, NOW + timedelta(seconds=3), 3.0)


def test_dwell_resets_when_touch_stops_holding() -> None:
    """插针弹回后必须重新计时，不能把两次短暂触碰累加成一次有效触碰。"""
    state = _state()
    _provisional_dwell_ready(state, "ETH_USDT", True, NOW, 3.0)
    assert not _provisional_dwell_ready(
        state, "ETH_USDT", False, NOW + timedelta(seconds=1), 3.0
    )
    assert "ETH_USDT" not in state.provisional_touch_since
    # 重新成立后再等满3秒
    assert not _provisional_dwell_ready(
        state, "ETH_USDT", True, NOW + timedelta(seconds=2), 3.0
    )
    assert not _provisional_dwell_ready(
        state, "ETH_USDT", True, NOW + timedelta(seconds=4), 3.0
    )
    assert _provisional_dwell_ready(state, "ETH_USDT", True, NOW + timedelta(seconds=5), 3.0)


def test_dwell_uses_elapsed_time_not_call_count() -> None:
    """轮次可能被跳过或变慢，"评估了三次"不等于"成立了三秒"。"""
    state = _state()
    _provisional_dwell_ready(state, "ETH_USDT", True, NOW, 3.0)
    # 只评估了第二次，但已经过了5秒
    assert _provisional_dwell_ready(state, "ETH_USDT", True, NOW + timedelta(seconds=5), 3.0)


def test_zero_dwell_fires_immediately() -> None:
    """停留时长配成0等于关掉这道闸，用于对照实验。"""
    state = _state()
    assert _provisional_dwell_ready(state, "ETH_USDT", True, NOW, 0.0)


def _micro(periods: int = 66, end: str = "2026-01-01 12:00") -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(end, tz="UTC"), periods=periods, freq="1min")
    return pd.DataFrame(
        {"open": [100.0] * periods, "high": [100.5] * periods, "low": [99.5] * periods,
         "close": [100.0] * periods, "volume": [10.0] * periods,
         "quote_volume": [1000.0] * periods},
        index=index,
    )


def _armed(state_path: Path, bars_by_interval, micro) -> float:
    """建账并把15分钟箱体打开，返回下轨价位。"""
    run_multi_timeframe_paper_cycle(
        bars_by_interval, micro_bars_by_symbol={"ETH_USDT": micro}, state_path=state_path
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["box_active"]["ETH_USDT"]["15m"] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")
    context = build_rotation_box_context(
        bars_by_interval["15m"], parameters_for_same_timeframe("15m")
    )
    return float(context.iloc[-1]["bb_lower"])


def _provisional_row(moment: pd.Timestamp, low: float, high: float) -> pd.Series:
    return pd.Series(
        {"open": low + 0.2, "high": high, "low": low, "close": low + 0.2,
         "volume": 0.0, "quote_volume": 0.0},
        name=moment,
    )


def test_provisional_touch_does_not_advance_the_cursor(tmp_path) -> None:
    """在途K线处理完后游标必须停在原地，否则这一分钟收线时会被整根跳过。"""
    bars = _bars_by_interval()
    state_path = tmp_path / "cursor.json"
    micro = _micro()
    lower = _armed(state_path, bars, micro)
    before = json.loads(state_path.read_text(encoding="utf-8"))["last_micro_bar_times"]["ETH_USDT"]

    moment = pd.Timestamp("2026-01-01 12:01", tz="UTC")
    run_multi_timeframe_paper_cycle(
        bars,
        micro_bars_by_symbol={"ETH_USDT": micro},
        provisional_by_symbol={"ETH_USDT": _provisional_row(moment, lower - 0.4, lower + 0.6)},
        provisional_dwell_seconds=0.0,
        now=NOW,
        state_path=state_path,
    )
    after = json.loads(state_path.read_text(encoding="utf-8"))["last_micro_bar_times"]["ETH_USDT"]
    assert after == before


def test_short_touch_is_filtered_by_dwell(tmp_path) -> None:
    """一秒的插针不该开仓——这正是加停留确认的目的。"""
    bars = _bars_by_interval()
    state_path = tmp_path / "spike.json"
    micro = _micro()
    lower = _armed(state_path, bars, micro)
    moment = pd.Timestamp("2026-01-01 12:01", tz="UTC")
    summary = run_multi_timeframe_paper_cycle(
        bars,
        micro_bars_by_symbol={"ETH_USDT": micro},
        provisional_by_symbol={"ETH_USDT": _provisional_row(moment, lower - 0.4, lower + 0.6)},
        provisional_dwell_seconds=3.0,
        now=NOW,
        state_path=state_path,
    )
    assert summary.position_side == ""
    assert not [event for event in summary.events if "模拟开仓" in event.title]


def test_sustained_touch_opens_and_records_the_bar(tmp_path) -> None:
    """连续成立满时长后开仓，并记下这一仓属于哪根K线。"""
    bars = _bars_by_interval()
    state_path = tmp_path / "sustained.json"
    micro = _micro()
    lower = _armed(state_path, bars, micro)
    moment = pd.Timestamp("2026-01-01 12:01", tz="UTC")
    row = _provisional_row(moment, lower - 0.4, lower + 0.6)
    kwargs = {
        "micro_bars_by_symbol": {"ETH_USDT": micro},
        "provisional_by_symbol": {"ETH_USDT": row},
        "provisional_dwell_seconds": 3.0,
        "state_path": state_path,
    }
    run_multi_timeframe_paper_cycle(bars, now=NOW, **kwargs)
    summary = run_multi_timeframe_paper_cycle(bars, now=NOW + timedelta(seconds=4), **kwargs)
    assert summary.position_side == "LONG"
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["opened_in_bar"]["ETH_USDT"] == moment.isoformat()
    # 成交后清空计时，避免下一次触轨直接被当成已经站住
    assert "ETH_USDT" not in stored["provisional_touch_since"]


def test_entry_bar_close_does_not_stop_on_pre_entry_prices(tmp_path) -> None:
    """在途开仓后，这一根收线时带回的完整极值可能发生在开仓之前。

    12:01:05 摸到下轨开仓、止损设在下方；而 12:01:02 的价格更低。收线K线的 low 记着
    那个更低的价格——用它止损等于拿入场前的价格把仓位打掉，这一仓从来没有真正亏到
    那个位置。
    """
    bars = _bars_by_interval()
    state_path = tmp_path / "preentry.json"
    micro = _micro()
    lower = _armed(state_path, bars, micro)
    moment = pd.Timestamp("2026-01-01 12:01", tz="UTC")
    row = _provisional_row(moment, lower - 0.4, lower + 0.6)
    kwargs = {
        "micro_bars_by_symbol": {"ETH_USDT": micro},
        "provisional_by_symbol": {"ETH_USDT": row},
        "provisional_dwell_seconds": 3.0,
        "state_path": state_path,
    }
    run_multi_timeframe_paper_cycle(bars, now=NOW, **kwargs)
    opened = run_multi_timeframe_paper_cycle(bars, now=NOW + timedelta(seconds=4), **kwargs)
    assert opened.position_side == "LONG"
    stop_price = json.loads(state_path.read_text(encoding="utf-8"))[
        "positions"]["ETH_USDT"]["stop_price"]

    # 这一根收线，low 落在止损之下——但那是开仓之前发生的。
    closed = micro.copy()
    closed.loc[moment] = {
        "open": lower + 0.2, "high": lower + 0.6, "low": stop_price - 5.0,
        "close": lower + 0.2, "volume": 10.0, "quote_volume": 1000.0,
    }
    summary = run_multi_timeframe_paper_cycle(
        bars,
        micro_bars_by_symbol={"ETH_USDT": closed},
        provisional_by_symbol=None,
        state_path=state_path,
        now=NOW + timedelta(seconds=70),
    )
    assert summary.position_side == "LONG", "开仓那一根的入场前极值不该把仓位止损掉"
    assert not [event for event in summary.events if "止损" in event.title]


def test_spike_that_returns_inside_never_opens(tmp_path) -> None:
    """一秒的插针把累计high顶出轨道后价格弹回——即使等满3秒也不能开仓。

    在途K线的high/low是一分钟内的累计最值，只增不减。拿它判定停留的话，插针一旦
    发生条件就永远成立，闸门只会把开仓延后几秒，完全起不到过滤作用。必须看当前价。
    """
    bars = _bars_by_interval()
    state_path = tmp_path / "returned.json"
    micro = _micro()
    lower = _armed(state_path, bars, micro)
    moment = pd.Timestamp("2026-01-01 12:01", tz="UTC")

    # low 已经跌破下轨（插针留下的累计最低价），但 close 已经回到轨道之内。
    spiked = pd.Series(
        {"open": lower + 0.5, "high": lower + 1.0, "low": lower - 0.4,
         "close": lower + 0.5, "volume": 0.0, "quote_volume": 0.0},
        name=moment,
    )
    kwargs = {
        "micro_bars_by_symbol": {"ETH_USDT": micro},
        "provisional_by_symbol": {"ETH_USDT": spiked},
        "provisional_dwell_seconds": 3.0,
        "state_path": state_path,
    }
    run_multi_timeframe_paper_cycle(bars, now=NOW, **kwargs)
    summary = run_multi_timeframe_paper_cycle(bars, now=NOW + timedelta(seconds=10), **kwargs)
    assert summary.position_side == "", "价格已弹回轨道内，插针不该开仓"
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert "ETH_USDT" not in stored["provisional_touch_since"], "条件不成立时必须清零"
