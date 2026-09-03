"""盘口聚合逻辑测试；全部为纯内存计算，不触库也不联网。"""

from datetime import UTC, datetime

from gold_crypto_quant.runtime.quote_collector import (
    QuoteBucket,
    QuoteCollector,
    floor_to_minute,
    floor_to_second,
)

MOMENT = datetime(2026, 9, 3, 14, 30, 45, 123456, tzinfo=UTC)


def test_bucket_keeps_only_extremes() -> None:
    """桶只保极值：触轨问的是"有没有碰到轨道"，桶内路径对该判定没有信息量。"""
    bucket = QuoteBucket.start(100.0, 100.5)
    bucket.fold(99.0, 99.5)
    bucket.fold(101.0, 101.5)
    bucket.fold(100.2, 100.7)

    assert (bucket.bid_low, bucket.bid_high) == (99.0, 101.0)
    assert (bucket.ask_low, bucket.ask_high) == (99.5, 101.5)
    assert bucket.frame_count == 4


def test_merge_equals_direct_aggregation() -> None:
    """秒桶合并成分钟桶，结果必须与直接聚合原始帧完全一致。

    这是"分钟级由秒级推导"的正确性前提；两者若不等，两张表就会互相矛盾。
    """
    frames = [(100.0, 100.5), (98.0, 98.5), (103.0, 103.5), (99.0, 99.5), (101.0, 101.5)]

    direct = QuoteBucket.start(*frames[0])
    for bid, ask in frames[1:]:
        direct.fold(bid, ask)

    first = QuoteBucket.start(*frames[0])
    first.fold(*frames[1])
    second = QuoteBucket.start(*frames[2])
    second.fold(*frames[3])
    second.fold(*frames[4])
    first.merge(second)

    assert (first.bid_low, first.bid_high) == (direct.bid_low, direct.bid_high)
    assert (first.ask_low, first.ask_high) == (direct.ask_low, direct.ask_high)
    assert first.frame_count == direct.frame_count


def test_floor_helpers_trim_to_bucket_start() -> None:
    assert floor_to_second(MOMENT) == datetime(2026, 9, 3, 14, 30, 45, tzinfo=UTC)
    assert floor_to_minute(MOMENT) == datetime(2026, 9, 3, 14, 30, tzinfo=UTC)


def _collector() -> QuoteCollector:
    collector = QuoteCollector(engine=object())
    collector._instrument_ids = {("GATE_LIVE_PUBLIC", "BTC_USDT"): 1}
    return collector


def test_only_completed_buckets_are_drained() -> None:
    """当前这一秒还在收帧，不能落盘；否则会写出半截极值再被覆盖。"""
    collector = _collector()
    key = ("GATE_LIVE_PUBLIC", "BTC_USDT")
    done = datetime(2026, 9, 3, 14, 30, 44, tzinfo=UTC)
    running = datetime(2026, 9, 3, 14, 30, 45, tzinfo=UTC)
    collector._seconds[(*key, done)] = QuoteBucket.start(100.0, 100.5)
    collector._seconds[(*key, running)] = QuoteBucket.start(101.0, 101.5)

    drained = collector.drain_seconds(MOMENT)

    assert len(drained) == 1
    assert drained[0][2] == datetime(2026, 9, 3, 14, 30, 44, tzinfo=UTC)
    # 未走完的那一秒必须留在内存里继续累积。
    assert len(collector._seconds) == 1


def test_minute_row_reports_how_many_seconds_were_covered() -> None:
    """covered_seconds 小于60就说明这一分钟断过线；回测据此排除不完整的分钟。

    盘口断线无法事后补齐——交易所不提供盘口历史，所以缺口必须显式标出来。
    """
    collector = _collector()
    key = ("GATE_LIVE_PUBLIC", "BTC_USDT")
    for second in (10, 11, 12):
        moment = datetime(2026, 9, 3, 14, 30, second, tzinfo=UTC)
        collector._seconds[(*key, moment)] = QuoteBucket.start(100.0 + second, 100.5 + second)
    collector.drain_seconds(datetime(2026, 9, 3, 14, 30, 20, tzinfo=UTC))

    minutes = collector.drain_minutes(datetime(2026, 9, 3, 14, 31, 5, tzinfo=UTC))

    assert len(minutes) == 1
    _venue, _contract, bucket_time, bucket, covered = minutes[0]
    assert bucket_time == datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    assert covered == 3
    assert bucket.bid_low == 110.0
    assert bucket.bid_high == 112.0
    assert bucket.frame_count == 3


def test_record_folds_frames_into_current_second() -> None:
    """同一秒内的多帧必须并进同一个桶，而不是各写一行。"""
    collector = _collector()
    collector.record("GATE_LIVE_PUBLIC", "BTC_USDT", 100.0, 100.5)
    collector.record("GATE_LIVE_PUBLIC", "BTC_USDT", 99.0, 99.5)

    assert len(collector._seconds) == 1
    bucket = next(iter(collector._seconds.values()))
    assert bucket.frame_count == 2
    assert bucket.bid_low == 99.0


def test_close_snapshot_tracks_the_last_frame() -> None:
    """展示当前价必须用同一帧的快照。

    极值来自秒内不同瞬间：拿"该秒最高买价"配"该秒最低卖价"会得到买一高于卖一的
    交叉盘口——这在真实盘口上不可能出现。
    """
    bucket = QuoteBucket.start(100.0, 100.1)
    bucket.fold(103.0, 103.1)
    bucket.fold(99.0, 99.1)

    # 极值确实是交叉的：最高买 103.0 > 最低卖 99.1。
    assert bucket.bid_high > bucket.ask_low
    # 快照则始终是一致的买卖对。
    assert (bucket.bid_close, bucket.ask_close) == (99.0, 99.1)
    assert bucket.ask_close > bucket.bid_close


def test_merge_keeps_the_later_snapshot() -> None:
    """秒桶汇总成分钟时，快照要取时间上更靠后的那个。"""
    earlier = QuoteBucket.start(100.0, 100.1)
    later = QuoteBucket.start(102.0, 102.1)
    earlier.merge(later)

    assert (earlier.bid_close, earlier.ask_close) == (102.0, 102.1)
