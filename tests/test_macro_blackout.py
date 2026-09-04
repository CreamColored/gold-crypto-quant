"""宏观数据静默窗口测试。

由来：2026-09-04 20:30:00 非农发布，BTC 一分钟内从 81285 砸到 80130（振幅1155点、
量放大12倍），影子账户在 20:30:3x 因"跌破下轨且连续3秒还在轨道外"判定为有效触碰，
开了两笔多单并在同一分钟内双双止损。

停留确认在这类行情里起反作用：它假设"持续触碰比瞬时插针可信"，而数据驱动的单边
启动里，持续待在轨道外恰恰说明压力还在。箱体的量比也拦不住——它看已收线K线，
爆量发生在当根之内，而在途K线没有成交量。

所以不从形态识别，改从时间规避。
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gold_crypto_quant.runtime.macro_blackout import (
    MacroEvent,
    active_blackout,
    load_events,
)


def test_nonfarm_is_the_first_friday_at_eight_thirty_eastern() -> None:
    """非农固定在每月第一个周五美东8:30。"""
    events = load_events(datetime(2026, 9, 4, 12, 30, tzinfo=UTC), config_path=Path("missing"))
    moments = {event.moment for event in events}
    # 2026-09-04 是九月第一个周五；夏令时下 8:30 ET = 12:30 UTC
    assert datetime(2026, 9, 4, 12, 30, tzinfo=UTC) in moments
    # 前后月份也要展开，否则跨月边界的窗口会漏
    assert datetime(2026, 8, 7, 12, 30, tzinfo=UTC) in moments
    assert datetime(2026, 10, 2, 12, 30, tzinfo=UTC) in moments


def test_daylight_saving_shifts_the_utc_moment() -> None:
    """8:30 ET 在夏令时是 12:30 UTC、冬令时是 13:30 UTC。

    写死 UTC 偏移会让窗口半年错整整一个小时——那时候静默窗口会完全落空，而且
    没有任何报错。
    """
    winter = load_events(datetime(2027, 1, 8, 13, 30, tzinfo=UTC), config_path=Path("missing"))
    # 2027-01-01 是一月第一个周五，冬令时
    assert datetime(2027, 1, 1, 13, 30, tzinfo=UTC) in {e.moment for e in winter}


def test_the_actual_losing_trades_fall_inside_the_window() -> None:
    """那两笔在 20:30:3x 开的多单必须落在静默窗口内。"""
    event = MacroEvent("美国非农就业", datetime(2026, 9, 4, 12, 30, tzinfo=UTC))
    hit = active_blackout(
        datetime(2026, 9, 4, 12, 30, 36, tzinfo=UTC), before=15, after=15, events=[event]
    )
    assert hit is not None and hit.name == "美国非农就业"


def test_window_covers_before_and_after() -> None:
    """发布前也要挡：数据出来前盘口就开始变薄，那时的触轨同样不是震荡回归。"""
    event = MacroEvent("非农", datetime(2026, 9, 4, 12, 30, tzinfo=UTC))
    inside = (
        datetime(2026, 9, 4, 12, 15, tzinfo=UTC),
        datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
        datetime(2026, 9, 4, 12, 45, tzinfo=UTC),
    )
    outside = (
        datetime(2026, 9, 4, 12, 14, 59, tzinfo=UTC),
        datetime(2026, 9, 4, 12, 45, 1, tzinfo=UTC),
    )
    for moment in inside:
        assert active_blackout(moment, before=15, after=15, events=[event]) is not None
    for moment in outside:
        assert active_blackout(moment, before=15, after=15, events=[event]) is None


def test_broken_config_falls_back_to_nonfarm() -> None:
    """配置损坏时不能静默失效——宁可只挡非农，也不能一个都不挡。"""
    events = load_events(
        datetime(2026, 9, 4, 12, 30, tzinfo=UTC), config_path=Path("/nonexistent/x.json")
    )
    assert any(event.name == "美国非农就业" for event in events)


def test_naive_datetime_is_rejected() -> None:
    """不带时区的时间会让整套换算失去意义，直接拒绝而不是猜。"""
    with pytest.raises(ValueError, match="timezone-aware"):
        active_blackout(datetime(2026, 9, 4, 12, 30), events=[])
