"""增量刷新的到期判断与并发抓取测试。

旧路径每轮把三个品种五个周期各拉一遍，实测单轮5.7秒里98%是15次串行HTTP往返，
而1小时线一小时才变一次——一天被白拉4300次。这里锁住两件事：到期周期按UTC自然
边界判定（重启不会错位），以及抓取失败时水位不能推进（否则该周期会静默缺K线）。
"""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from gold_crypto_quant.market_data.live_refresh import (
    due_intervals,
    last_closed_open_time,
    refresh_live_bars,
)

INTERVALS = ("1m", "5m", "15m", "30m", "1h")


def test_last_closed_bar_aligns_to_utc_boundaries() -> None:
    """收线时刻必须对齐UTC自然边界，而不是相对服务启动时间。"""
    now = datetime(2026, 9, 4, 4, 50, 17, tzinfo=UTC)
    assert last_closed_open_time("1m", now) == datetime(2026, 9, 4, 4, 49, tzinfo=UTC)
    assert last_closed_open_time("5m", now) == datetime(2026, 9, 4, 4, 45, tzinfo=UTC)
    assert last_closed_open_time("15m", now) == datetime(2026, 9, 4, 4, 30, tzinfo=UTC)
    assert last_closed_open_time("30m", now) == datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
    assert last_closed_open_time("1h", now) == datetime(2026, 9, 4, 3, 0, tzinfo=UTC)


def test_first_cycle_refreshes_every_interval() -> None:
    """没有水位时全部到期，保证冷启动和重启都能补齐。"""
    now = datetime(2026, 9, 4, 4, 50, 17, tzinfo=UTC)
    assert due_intervals(INTERVALS, now=now, refreshed={}) == INTERVALS


def test_twenty_second_polling_skips_two_cycles_in_three() -> None:
    """1分钟线60秒才收一根，20秒轮询下三轮里两轮无事可做。"""
    now = datetime(2026, 9, 4, 4, 50, 17, tzinfo=UTC)
    refreshed: dict[str, datetime] = {}
    observed = []
    for step in range(6):
        moment = now + timedelta(seconds=20 * step)
        due = due_intervals(INTERVALS, now=moment, refreshed=refreshed)
        observed.append(due)
        for interval in due:
            refreshed[interval] = last_closed_open_time(interval, moment)
    assert observed[0] == INTERVALS
    assert observed[1] == () and observed[2] == ()
    assert observed[3] == ("1m",)
    assert observed[4] == () and observed[5] == ()


def _frame() -> pd.DataFrame:
    index = pd.to_datetime(["2026-09-04T04:49:00Z"])
    return pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
         "volume": [10.0], "quote_volume": [15.0]},
        index=index,
    )


class _Instrument:
    id = 1


def _run(monkeypatch, *, fetch, refreshed, store=None, now=None, contracts=("BTC_USDT",)):
    """把落库和数据库连接都换成假的，只观察抓取计划与水位推进。"""
    calls = []

    def _store(session, frame, *, instrument_id, interval, now):  # noqa: ARG001
        calls.append(interval)
        if store is not None:
            store(interval)
        return 1, 0

    monkeypatch.setattr("gold_crypto_quant.market_data.live_refresh._store_frame", _store)
    monkeypatch.setattr("gold_crypto_quant.market_data.live_refresh.Session", _FakeSession)
    results = refresh_live_bars(
        fetch=fetch,
        ensure_instrument=lambda session, contract: _Instrument(),  # noqa: ARG005
        contracts=contracts,
        intervals=INTERVALS,
        refreshed=refreshed,
        engine=object(),
        now=now or datetime(2026, 9, 4, 4, 50, 17, tzinfo=UTC),
    )
    return results, calls


class _FakeSession:
    def __init__(self, engine):  # noqa: ARG002
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self) -> None:
        pass


def test_only_due_intervals_are_fetched(monkeypatch) -> None:
    """水位已经追平的周期不发请求——这正是省掉91%调用的地方。"""
    now = datetime(2026, 9, 4, 4, 50, 17, tzinfo=UTC)
    refreshed = {interval: last_closed_open_time(interval, now) for interval in INTERVALS}
    refreshed["1m"] = last_closed_open_time("1m", now - timedelta(minutes=1))
    fetched = []

    def fetch(contract, interval):
        fetched.append(interval)
        return _frame()

    _run(monkeypatch, fetch=fetch, refreshed=refreshed, now=now)
    assert fetched == ["1m"]


def test_nothing_due_skips_the_whole_pipeline(monkeypatch) -> None:
    """全部追平时连数据库会话都不该建立。"""
    now = datetime(2026, 9, 4, 4, 50, 17, tzinfo=UTC)
    refreshed = {interval: last_closed_open_time(interval, now) for interval in INTERVALS}

    def fetch(contract, interval):
        raise AssertionError("追平后不应再发请求")

    results, calls = _run(monkeypatch, fetch=fetch, refreshed=refreshed, now=now)
    assert results == [] and calls == []


def test_failed_store_does_not_advance_the_watermark(monkeypatch) -> None:
    """落库失败必须让该周期下一轮重来，否则会静默缺K线。"""
    refreshed: dict[str, datetime] = {}

    def store(interval):
        if interval == "5m":
            raise RuntimeError("模拟落库失败")

    with pytest.raises(RuntimeError):
        _run(monkeypatch, fetch=lambda c, i: _frame(), refreshed=refreshed, store=store)
    assert "5m" not in refreshed


def test_delisting_check_propagates(monkeypatch) -> None:
    """下架必须中止采集，不能因为放进线程池就被吞掉。"""
    def check(contract):
        raise RuntimeError(f"{contract} is delisting")

    monkeypatch.setattr("gold_crypto_quant.market_data.live_refresh._store_frame",
                        lambda *a, **k: (0, 0))
    monkeypatch.setattr("gold_crypto_quant.market_data.live_refresh.Session", _FakeSession)
    with pytest.raises(RuntimeError, match="delisting"):
        refresh_live_bars(
            fetch=lambda c, i: _frame(),
            ensure_instrument=lambda s, c: _Instrument(),  # noqa: ARG005
            check_contract=check,
            contracts=("BTC_USDT",),
            intervals=INTERVALS,
            refreshed={},
            engine=object(),
        )
