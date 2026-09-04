"""采集器写 Redis 现价快照的测试。

两条不变量：Redis 故障不能拖垮采集（真身在 MySQL，快照丢了下一秒补回来），以及
每个品种只写最后一秒（Redis 是快照不是流水，键数必须固定）。
"""

from datetime import UTC, datetime

from gold_crypto_quant.runtime import quote_collector
from gold_crypto_quant.runtime.quote_collector import QuoteBucket


class _Pipe:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def hset(self, key, mapping):
        self.sink.append((key, mapping))

    def execute(self):
        return []


class _Client:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def pipeline(self, transaction: bool = True):  # noqa: ARG002
        return _Pipe(self.sink)


class _Broken:
    def pipeline(self, transaction: bool = True):  # noqa: ARG002
        raise ConnectionError("redis down")


class _Collector:
    """只借用被测方法，避开采集器构造时的数据库依赖。"""

    _publish_snapshots = quote_collector.QuoteCollector._publish_snapshots

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.reporter = self.messages.append


def _drained() -> list[tuple]:
    early = QuoteBucket(1.0, 2.0, 3.0, 4.0, 10, bid_close=1.5, ask_close=3.5)
    late = QuoteBucket(5.0, 6.0, 7.0, 8.0, 20, bid_close=5.5, ask_close=7.5)
    return [
        ("GATE_LIVE_PUBLIC", "BTC_USDT", datetime(2026, 9, 4, 5, 0, 1, tzinfo=UTC), early),
        ("GATE_LIVE_PUBLIC", "BTC_USDT", datetime(2026, 9, 4, 5, 0, 2, tzinfo=UTC), late),
        ("GATE_LIVE_PUBLIC", "ETH_USDT", datetime(2026, 9, 4, 5, 0, 2, tzinfo=UTC), early),
    ]


def test_only_the_latest_second_is_published(monkeypatch) -> None:
    """同一品种多秒同时冲刷时只写最后一秒，键数才能固定为交易所×品种。"""
    sink: list = []
    monkeypatch.setattr(quote_collector, "build_redis", lambda: _Client(sink))
    _Collector()._publish_snapshots(_drained())
    assert len(sink) == 2
    written = dict(sink)
    btc = written["gcq:quote:GATE_LIVE_PUBLIC:BTC_USDT"]
    assert btc["bid"] == 5.5 and btc["ask"] == 7.5
    assert btc["ts"] == "2026-09-04T05:00:02+00:00"


def test_redis_failure_does_not_break_collection(monkeypatch) -> None:
    """Redis 挂掉只记一行日志；MySQL 那条链路必须照常走完。"""
    monkeypatch.setattr(quote_collector, "build_redis", lambda: _Broken())
    collector = _Collector()
    collector._publish_snapshots(_drained())
    assert len(collector.messages) == 1
    assert "Redis快照写入失败" in collector.messages[0]


def test_empty_drain_touches_nothing(monkeypatch) -> None:
    """没有走完的秒桶时不该建立连接。"""
    def _boom():
        raise AssertionError("空冲刷不应访问 Redis")

    monkeypatch.setattr(quote_collector, "build_redis", _boom)
    _Collector()._publish_snapshots([])


def test_timestamp_is_the_bucket_not_now(monkeypatch) -> None:
    """陈旧度靠这个时间戳判定，必须是桶的时刻而不是写入时刻。

    采集服务挂掉后 Redis 里的键还在、值还是旧的，读取端收不到任何错误——写入时刻
    会让"最后一次写"看起来永远很新，只有桶时刻能暴露数据停了。
    """
    sink: list = []
    monkeypatch.setattr(quote_collector, "build_redis", lambda: _Client(sink))
    _Collector()._publish_snapshots(_drained())
    for _key, mapping in sink:
        assert mapping["ts"].startswith("2026-09-04T05:00:0")


def test_second_quotes_are_no_longer_stored() -> None:
    """秒级盘口不再落库：一天97MB，而唯一需要历史的读者已被证明无差别。

    秒级回测与1m回测在固定窗口下逐字节相同——模拟器的成交价永远取轨道价，
    早一秒察觉不改变记录下来的成交。分钟级聚合保留，作为粗粒度留档。
    """
    assert quote_collector.STORE_SECOND_QUOTES is False


def test_flush_still_writes_minute_aggregates(monkeypatch) -> None:
    """秒级停写，分钟级必须照写——它是唯一剩下的盘口历史留档。"""
    written = []

    class _Collector:
        flush = quote_collector.QuoteCollector.flush
        _instrument_ids = {("V", "S"): 1}

        def drain_seconds(self, now):  # noqa: ARG002
            bucket = QuoteBucket(1.0, 2.0, 3.0, 4.0, 5, bid_close=1.5, ask_close=3.5)
            return [("V", "S", datetime(2026, 9, 4, 12, 0, 1, tzinfo=UTC), bucket)]

        def drain_minutes(self, now):  # noqa: ARG002
            bucket = QuoteBucket(1.0, 2.0, 3.0, 4.0, 5, bid_close=1.5, ask_close=3.5)
            return [("V", "S", datetime(2026, 9, 4, 12, 0, tzinfo=UTC), bucket, 60)]

        def _write(self, table, rows):
            written.append(table.__name__)

        def _publish_snapshots(self, drained):
            pass

    _Collector().flush(datetime(2026, 9, 4, 12, 1, tzinfo=UTC))
    assert "MarketQuoteSecond" not in written
    assert "MarketQuoteMinute" in written
