"""策略取数层测试。

最关键的一条：降级判据必须是陈旧度，不是读取失败。采集服务挂掉之后 Redis 里的键
还在、值还是旧的，读取端不会收到任何错误——用"读失败才切换"会让策略拿着十分钟前
的价格继续交易，而且一次告警都不发。
"""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from gold_crypto_quant.storage import bar_source
from gold_crypto_quant.storage.bar_source import SourceHealth, check_health, load_bars

NOW = datetime(2026, 9, 4, 6, 30, tzinfo=UTC)


def _cursor(age_seconds: float | None, **extra) -> dict:
    data = dict(extra)
    if age_seconds is not None:
        data["_heartbeat"] = (NOW - timedelta(seconds=age_seconds)).isoformat()
    return data


def test_fresh_heartbeat_uses_redis(monkeypatch) -> None:
    monkeypatch.setattr(bar_source, "read_cursor", lambda venue: _cursor(2.0))  # noqa: ARG005
    health = check_health("V", now=NOW)
    assert health.source == "redis" and not health.degraded


def test_stale_heartbeat_degrades_even_though_reads_succeed(monkeypatch) -> None:
    """键还在、读得到、值是旧的——这正是采集服务挂掉后的样子。"""
    monkeypatch.setattr(bar_source, "read_cursor", lambda venue: _cursor(600.0))  # noqa: ARG005
    health = check_health("V", now=NOW)
    assert health.degraded and "600" in health.reason


def test_missing_heartbeat_degrades(monkeypatch) -> None:
    """Redis 被清空或还没写过心跳时不能当作新鲜。"""
    monkeypatch.setattr(bar_source, "read_cursor", lambda venue: {})  # noqa: ARG005
    assert check_health("V", now=NOW).degraded


def test_unreachable_redis_degrades_instead_of_raising(monkeypatch) -> None:
    """Redis 挂掉应该让系统变慢，不应该让它停摆。"""
    def _boom(venue):  # noqa: ARG001
        raise ConnectionError("redis down")

    monkeypatch.setattr(bar_source, "read_cursor", _boom)
    health = check_health("V", now=NOW)
    assert health.degraded and "ConnectionError" in health.reason


def _frame(rows: int) -> pd.DataFrame:
    index = pd.date_range("2026-09-04", periods=rows, freq="1min", tz="UTC", name="open_time")
    return pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0,
         "quote_volume": 15.0, "provisional": False},
        index=index,
    )


def test_degraded_health_reads_mysql(monkeypatch) -> None:
    monkeypatch.setattr(bar_source, "read_bars", lambda *a, **k: pytest.fail("不该读Redis"))
    monkeypatch.setattr(bar_source, "load_market_bars", lambda *a, **k: _frame(300))
    health = SourceHealth("mysql", "心跳已停")
    assert len(load_bars("S", "1m", venue="V", limit=300, health=health)) == 300


def test_short_redis_series_falls_back(monkeypatch) -> None:
    """采集服务刚起、序列还没灌满时要退回 MySQL。

    布林带和MACD需要足够的预热长度，几十根算出来的轨道是错的——宁可慢一点读
    MySQL，也不能拿错误的轨道去开仓。
    """
    monkeypatch.setattr(bar_source, "read_bars", lambda *a, **k: _frame(30))
    monkeypatch.setattr(bar_source, "load_market_bars", lambda *a, **k: _frame(500))
    health = SourceHealth("redis", "")
    assert len(load_bars("S", "1m", venue="V", limit=500, health=health)) == 500


def test_redis_read_error_falls_back_to_mysql(monkeypatch) -> None:
    """单条序列读失败不该影响其它序列，也不该抛给策略。"""
    def _boom(*a, **k):  # noqa: ARG001
        raise RuntimeError("坏了")

    monkeypatch.setattr(bar_source, "read_bars", _boom)
    monkeypatch.setattr(bar_source, "load_market_bars", lambda *a, **k: _frame(500))
    assert len(load_bars("S", "1m", venue="V", limit=500, health=SourceHealth("redis", ""))) == 500


def test_provisional_column_is_stripped(monkeypatch) -> None:
    """策略拿到的列必须与 MySQL 路径一致，否则两条路径行为会分叉。"""
    monkeypatch.setattr(bar_source, "read_bars", lambda *a, **k: _frame(500))
    frame = load_bars("S", "1m", venue="V", limit=500, health=SourceHealth("redis", ""))
    assert "provisional" not in frame.columns


def test_bars_are_cached_until_the_cursor_moves(monkeypatch) -> None:
    """收线K线一分钟才变一次，而策略每秒都跑；水位没动就不该重读。"""
    bar_source.reset_bars_cache()
    reads = []

    def _read(*a, **k):  # noqa: ARG001
        reads.append(1)
        return _frame(300)

    monkeypatch.setattr(bar_source, "read_bars", _read)
    health = SourceHealth("redis", "", 1.0, {"S:1m": "2026-09-04T06:30:00+00:00"})
    for _ in range(5):
        load_bars("S", "1m", venue="V", limit=300, health=health)
    assert len(reads) == 1


def test_new_cursor_forces_a_reread(monkeypatch) -> None:
    """新K线收线后必须重读，否则策略会拿着上一分钟的数据。"""
    bar_source.reset_bars_cache()
    reads = []

    def _read(*a, **k):  # noqa: ARG001
        reads.append(1)
        return _frame(300)

    monkeypatch.setattr(bar_source, "read_bars", _read)
    for stamp in ("2026-09-04T06:30:00+00:00", "2026-09-04T06:31:00+00:00"):
        load_bars("S", "1m", venue="V", limit=300,
                  health=SourceHealth("redis", "", 1.0, {"S:1m": stamp}))
    assert len(reads) == 2


def test_missing_cursor_token_never_caches(monkeypatch) -> None:
    """没有水位就无法判断数据是否变过，宁可每次重读也不能拿旧数据冒充新的。"""
    bar_source.reset_bars_cache()
    reads = []

    def _read(*a, **k):  # noqa: ARG001
        reads.append(1)
        return _frame(300)

    monkeypatch.setattr(bar_source, "read_bars", _read)
    health = SourceHealth("redis", "", 1.0, {})
    for _ in range(3):
        load_bars("S", "1m", venue="V", limit=300, health=health)
    assert len(reads) == 3
