"""在途K线累加器测试。

时间归属必须用交易所时间戳：本机时钟和交易所有偏差，跨代理更明显，用本机时钟会让
K线边界错位——一帧属于哪一分钟判错，这根K线的开盘价就错了。
"""

from datetime import UTC, datetime

from gold_crypto_quant.runtime.provisional_bars import ProvisionalMinute, ProvisionalTracker


def test_folds_within_the_same_minute() -> None:
    tracker = ProvisionalTracker()
    for second, price in ((1, 100.0), (2, 105.0), (3, 98.0), (4, 101.0)):
        tracker.observe("V", "S", price, datetime(2026, 9, 4, 6, 30, second, tzinfo=UTC))
    bar = tracker.snapshot()[("V", "S")]
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 98.0, 101.0)
    assert bar.open_time == datetime(2026, 9, 4, 6, 30, tzinfo=UTC)
    assert bar.provisional


def test_new_minute_starts_a_fresh_bar() -> None:
    """跨分钟必须翻页，不能把上一分钟的极值带进来。"""
    tracker = ProvisionalTracker()
    tracker.observe("V", "S", 100.0, datetime(2026, 9, 4, 6, 30, 59, tzinfo=UTC))
    tracker.observe("V", "S", 200.0, datetime(2026, 9, 4, 6, 31, 0, tzinfo=UTC))
    bar = tracker.snapshot()[("V", "S")]
    assert bar.open_time == datetime(2026, 9, 4, 6, 31, tzinfo=UTC)
    assert (bar.open, bar.high, bar.low) == (200.0, 200.0, 200.0)


def test_venues_and_symbols_do_not_mix() -> None:
    tracker = ProvisionalTracker()
    moment = datetime(2026, 9, 4, 6, 30, 1, tzinfo=UTC)
    tracker.observe("GATE", "BTC", 100.0, moment)
    tracker.observe("BINANCE", "BTC", 200.0, moment)
    tracker.observe("GATE", "ETH", 300.0, moment)
    snapshot = tracker.snapshot()
    assert len(snapshot) == 3
    assert snapshot[("GATE", "BTC")].close == 100.0
    assert snapshot[("BINANCE", "BTC")].close == 200.0


def test_seed_restores_a_partial_bar() -> None:
    """重启补齐的那根必须能继续累加，而不是被下一帧重置。"""
    tracker = ProvisionalTracker()
    minute = datetime(2026, 9, 4, 6, 30, tzinfo=UTC)
    tracker.seed("V", "S", ProvisionalMinute(minute, 50.0, 60.0, 40.0, 55.0))
    tracker.observe("V", "S", 70.0, datetime(2026, 9, 4, 6, 30, 41, tzinfo=UTC))
    bar = tracker.snapshot()[("V", "S")]
    assert bar.open == 50.0 and bar.high == 70.0 and bar.low == 40.0 and bar.close == 70.0


def test_provisional_bar_carries_no_volume() -> None:
    """盘口没有成交量；编一个数字会让人以为它有意义。"""
    tracker = ProvisionalTracker()
    tracker.observe("V", "S", 100.0, datetime(2026, 9, 4, 6, 30, 1, tzinfo=UTC))
    bar = tracker.snapshot()[("V", "S")]
    assert bar.volume == 0.0 and bar.quote_volume == 0.0
