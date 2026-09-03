"""并发写入K线时的死锁重试测试。

Gate与币安并行拉取会同时往 market_bars 批量 upsert，InnoDB 在唯一索引上的间隙锁
可能交叉成死锁。并行上线后实测八九分钟就发生一次，而不重试的话一次死锁会整轮
杀掉该交易所的周期——连策略都不会跑。
"""

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError

from gold_crypto_quant.market_data import gate_history
from gold_crypto_quant.market_data.gate_history import (
    DEADLOCK_RETRIES,
    _is_retryable_lock_error,
    _store_frame,
)


class _Orig(Exception):
    def __init__(self, code: int) -> None:
        self.args = (code, "mysql error")


def _error(code: int) -> OperationalError:
    return OperationalError("INSERT ...", {}, _Orig(code))


class _Session:
    """只记录调用次数的假会话；不连数据库。"""

    def __init__(self, fail_times: int, code: int = 1213) -> None:
        self.fail_times = fail_times
        self.code = code
        self.commits = 0
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10.0]},
        index=pd.date_range("2026-01-01", periods=1, freq="1min", tz="UTC"),
    )


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(gate_history, "DEADLOCK_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(
        gate_history, "_build_bar_rows", lambda *_a, **_k: ([{"x": 1}], 0)
    )


def test_deadlock_is_retried_until_it_succeeds(patched, monkeypatch) -> None:
    """死锁后回滚重试；MySQL 的报错本身就写着 try restarting transaction。"""
    session = _Session(fail_times=2)
    calls = {"n": 0}

    def upsert(_session, rows):
        calls["n"] += 1
        if calls["n"] <= session.fail_times:
            raise _error(1213)
        return len(rows)

    monkeypatch.setattr(gate_history, "_upsert_bars", upsert)

    stored, skipped = _store_frame(
        session, _frame(), instrument_id=1, interval="1m", now=pd.Timestamp.utcnow()
    )

    assert (stored, skipped) == (1, 0)
    assert calls["n"] == 3
    assert session.rollbacks == 2


def test_persistent_deadlock_finally_raises(patched, monkeypatch) -> None:
    """一直死锁最终仍要抛出，不能无限重试把整轮拖死。"""
    session = _Session(fail_times=99)
    monkeypatch.setattr(
        gate_history, "_upsert_bars", lambda *_a, **_k: (_ for _ in ()).throw(_error(1213))
    )

    with pytest.raises(OperationalError):
        _store_frame(
            session, _frame(), instrument_id=1, interval="1m", now=pd.Timestamp.utcnow()
        )
    assert session.rollbacks == DEADLOCK_RETRIES - 1


def test_non_lock_errors_are_not_retried(patched, monkeypatch) -> None:
    """表不存在、认证失败这类错误重试没有意义，必须立刻抛出。"""
    session = _Session(fail_times=99)
    calls = {"n": 0}

    def upsert(*_a, **_k):
        calls["n"] += 1
        raise _error(1146)

    monkeypatch.setattr(gate_history, "_upsert_bars", upsert)

    with pytest.raises(OperationalError):
        _store_frame(
            session, _frame(), instrument_id=1, interval="1m", now=pd.Timestamp.utcnow()
        )
    assert calls["n"] == 1
    assert session.rollbacks == 0


def test_lock_wait_timeout_counts_as_retryable() -> None:
    """1205 锁等待超时与 1213 死锁同因，都应重试。"""
    assert _is_retryable_lock_error(_error(1213)) is True
    assert _is_retryable_lock_error(_error(1205)) is True
    assert _is_retryable_lock_error(_error(1146)) is False
