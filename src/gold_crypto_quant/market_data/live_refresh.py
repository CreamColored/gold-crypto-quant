"""实盘对照专用的增量K线刷新。

旧路径每轮把三个品种五个周期各拉一遍，一天里1小时线被重复拉4300次，而它一小时
才变一次。实测单次HTTP往返约410毫秒且与数据量无关（limit=5和limit=1000都是410
毫秒），所以省流量没有意义，**少发请求**才有意义。

这里做两件事：
1. 只拉"自上次刷新以来又收出新K线"的周期；
2. 网络并发、落库串行。

第二条是刻意的：并发写MySQL正是此前死锁1213的来源，而HTTP等待才是真正的瓶颈。
把两者拆开既拿到并发收益，又不额外制造锁竞争。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gold_crypto_quant.market_data.gate_history import INTERVAL_DURATION, _store_frame
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class LiveRefreshResult:
    """单个品种单个周期的刷新结果，字段与既有ImportResult保持同名便于汇总。"""

    contract: str
    interval: str
    fetched: int
    stored: int
    skipped_open: int


def last_closed_open_time(interval: str, now: datetime) -> datetime:
    """该周期最近一根**已收线**K线的open_time，按UTC自然边界对齐。

    交易所的K线边界是UTC整点整分，不是"服务启动后每N分钟"，因此这里用纪元偏移
    取整而不是相对时间，重启后不会错位。
    """
    duration = INTERVAL_DURATION[interval]
    floored = _EPOCH + ((now - _EPOCH) // duration) * duration
    return floored - duration


def due_intervals(
    intervals: Iterable[str],
    *,
    now: datetime,
    refreshed: Mapping[str, datetime],
) -> tuple[str, ...]:
    """挑出自上次刷新以来又收出新K线的周期；没刷过的周期一律算到期。"""
    due = []
    for interval in intervals:
        latest = last_closed_open_time(interval, now)
        previous = refreshed.get(interval)
        if previous is None or latest > previous:
            due.append(interval)
    return tuple(due)


def refresh_live_bars(
    *,
    fetch: Callable[[str, str], pd.DataFrame],
    ensure_instrument: Callable[[Session, str], Instrument],
    contracts: tuple[str, ...],
    intervals: tuple[str, ...],
    refreshed: MutableMapping[str, datetime],
    check_contract: Callable[[str], None] | None = None,
    engine: Engine | None = None,
    now: datetime | None = None,
    max_workers: int = 8,
) -> list[LiveRefreshResult]:
    """只刷新到期周期；HTTP并发、落库串行。

    ``refreshed`` 会被原地更新，调用方每个交易所持有一份。某个周期只要有一个品种
    抓取失败就不推进该周期的水位，下一轮整体重来——宁可多拉一次，也不能让某个品种
    静默缺K线。
    """
    run_now = now or datetime.now(UTC)
    due = due_intervals(intervals, now=run_now, refreshed=refreshed)
    if not due:
        return []

    engine = engine or build_engine()
    jobs = [(contract, interval) for contract in contracts for interval in due]

    # 下架检查和K线抓取都是纯网络调用，一起并发；两者都不碰数据库。
    def fetch_one(job: tuple[str, str]) -> tuple[str, str, pd.DataFrame]:
        contract, interval = job
        return contract, interval, fetch(contract, interval)

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="refresh") as pool:
        contract_checks = (
            [pool.submit(check_contract, contract) for contract in contracts]
            if check_contract is not None
            else []
        )
        fetched = list(pool.map(fetch_one, jobs))
        # 下架属于必须中止采集的情况，异常照常向上抛。
        for check in contract_checks:
            check.result()

    results: list[LiveRefreshResult] = []
    failed_intervals: set[str] = set()
    with Session(engine) as session:
        instrument_ids: dict[str, int] = {}
        for contract in contracts:
            instrument_ids[contract] = ensure_instrument(session, contract).id
        session.commit()
        for contract, interval, frame in fetched:
            try:
                stored, skipped_open = _store_frame(
                    session,
                    frame,
                    instrument_id=instrument_ids[contract],
                    interval=interval,
                    now=run_now,
                )
            except Exception:
                failed_intervals.add(interval)
                raise
            results.append(
                LiveRefreshResult(
                    contract=contract,
                    interval=interval,
                    fetched=len(frame),
                    stored=stored,
                    skipped_open=skipped_open,
                )
            )

    for interval in due:
        if interval not in failed_intervals:
            refreshed[interval] = last_closed_open_time(interval, run_now)
    return results
