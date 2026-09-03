"""下载 Gate 测试网历史K线并幂等写入 MySQL。"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from gold_crypto_quant.exchanges.gate import SUPPORTED_INTERVALS, GateTestnetClient
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, MarketBar

GATE_TESTNET_VENUE = "GATE_TESTNET"
GATE_LIVE_VENUE = "GATE_LIVE_PUBLIC"
DEFAULT_CONTRACTS = ("BTC_USDT", "ETH_USDT")
DEFAULT_INTERVALS = ("5m", "15m", "30m", "1h")
# Gate测试网会拒绝早于“最近10000个点”的K线范围；预留两个周期处理当前未收盘边界。
GATE_RECENT_POINT_LIMIT = 10_000

# 用固定映射计算K线收盘时间，避免字符串解析差异影响“是否已经收盘”的判断。
INTERVAL_DURATION = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
}


@dataclass(frozen=True, slots=True)
class ImportResult:
    """一个合约、一个周期的导入统计。"""

    contract: str
    interval: str
    fetched: int
    stored: int
    skipped_open: int
    pages: int
    history_limited: bool


def _split_contract(contract: str) -> tuple[str, str]:
    """把 Gate 的 ``BTC_USDT`` 拆成基础资产和计价资产。"""
    try:
        base_asset, quote_asset = contract.split("_", maxsplit=1)
    except ValueError as exc:
        raise ValueError(f"invalid Gate contract name: {contract}") from exc
    if not base_asset or not quote_asset:
        raise ValueError(f"invalid Gate contract name: {contract}")
    return base_asset, quote_asset


def _ensure_instrument(
    session: Session,
    contract: str,
    *,
    venue: str = GATE_TESTNET_VENUE,
) -> Instrument:
    """登记或重新启用 Gate 合约，并返回数据库中的品种对象。"""
    base_asset, quote_asset = _split_contract(contract)
    # MySQL ON DUPLICATE KEY UPDATE 让初始化可以安全重复执行，不会产生重复品种。
    statement = mysql_insert(Instrument).values(
        venue=venue,
        symbol=contract,
        asset_class="CRYPTO_FUTURES",
        base_asset=base_asset,
        quote_asset=quote_asset,
        is_active=True,
    )
    session.execute(statement.on_duplicate_key_update(is_active=True))
    session.flush()

    # 通过唯一业务键重新查询，避免依赖“插入或更新”两种情况下不同的自增ID行为。
    return session.execute(
        select(Instrument).where(
            Instrument.venue == venue,
            Instrument.symbol == contract,
        )
    ).scalar_one()


def _build_bar_rows(
    frame: pd.DataFrame,
    *,
    instrument_id: int,
    interval: str,
    now: datetime | None = None,
) -> tuple[list[dict[str, object]], int]:
    """把标准化 DataFrame 转成数据库记录，并过滤当前尚未收盘的K线。"""
    if interval not in INTERVAL_DURATION:
        raise ValueError(f"unsupported interval: {interval}")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    duration = INTERVAL_DURATION[interval]
    rows: list[dict[str, object]] = []
    skipped_open = 0
    for open_time, candle in frame.iterrows():
        # Pandas Timestamp 转 Python datetime 后仍保留 UTC 时区，用于可靠比较。
        aware_open_time = open_time.to_pydatetime()
        close_time = aware_open_time + duration
        if close_time > now:
            skipped_open += 1
            continue

        # MySQL DATETIME 不保存时区标记；写入前去掉 tzinfo，但其数值语义始终是 UTC。
        database_open_time = aware_open_time.astimezone(UTC).replace(tzinfo=None)
        database_close_time = close_time.astimezone(UTC).replace(tzinfo=None)
        rows.append(
            {
                "instrument_id": instrument_id,
                "interval_code": interval,
                "open_time": database_open_time,
                "close_time": database_close_time,
                "open_price": Decimal(str(candle["open"])),
                "high_price": Decimal(str(candle["high"])),
                "low_price": Decimal(str(candle["low"])),
                "close_price": Decimal(str(candle["close"])),
                "volume": Decimal(str(candle["volume"])),
                "quote_volume": Decimal(str(candle["quote_volume"])),
                # Gate 当前不提供该字段；币安标准化K线包含成交笔数时一并保存。
                "trade_count": (
                    int(candle["trade_count"])
                    if "trade_count" in candle and pd.notna(candle["trade_count"])
                    else None
                ),
                "is_closed": True,
            }
        )
    return rows, skipped_open


def _upsert_bars(session: Session, rows: list[dict[str, object]]) -> int:
    """批量新增K线；已存在的唯一键只更新OHLCV，不重复插入。"""
    if not rows:
        return 0

    statement = mysql_insert(MarketBar).values(rows)
    # Gate 可能修正最近一根已收盘K线，所以幂等重跑时允许刷新行情字段。
    update_statement = statement.on_duplicate_key_update(
        close_time=statement.inserted.close_time,
        open_price=statement.inserted.open_price,
        high_price=statement.inserted.high_price,
        low_price=statement.inserted.low_price,
        close_price=statement.inserted.close_price,
        volume=statement.inserted.volume,
        quote_volume=statement.inserted.quote_volume,
        trade_count=statement.inserted.trade_count,
        is_closed=statement.inserted.is_closed,
    )
    session.execute(update_statement)
    return len(rows)


def _earliest_bar_time(session: Session, instrument_id: int, interval: str) -> datetime | None:
    """查询当前数据库中某品种周期最早的K线时间，用作断点续传游标。"""
    statement = select(func.min(MarketBar.open_time)).where(
        MarketBar.instrument_id == instrument_id,
        MarketBar.interval_code == interval,
    )
    # 调用scalar执行单值查询；没有历史数据时数据库返回None。
    return session.execute(statement).scalar_one_or_none()


# Gate与币安并行拉取时会同时往 market_bars 批量upsert，InnoDB 在唯一索引上的
# 间隙锁可能交叉成死锁。MySQL 的报错本身就写着 try restarting transaction——
# 死锁是并发写入的正常现象，输的那一方回滚重试即可。不重试的话一次死锁会整轮
# 杀掉该交易所的周期，连策略都不会跑：并行上线后八九分钟就发生一次。
DEADLOCK_RETRIES = 4
DEADLOCK_BACKOFF_SECONDS = 0.05


def _is_retryable_lock_error(error: OperationalError) -> bool:
    """识别 MySQL 1213 死锁与 1205 锁等待超时；两者都应当回滚重试。"""
    args = getattr(getattr(error, "orig", None), "args", ())
    return bool(args) and args[0] in {1213, 1205}


def _store_frame(
    session: Session,
    frame: pd.DataFrame,
    *,
    instrument_id: int,
    interval: str,
    now: datetime,
) -> tuple[int, int]:
    """转换、幂等写入并提交单页K线，保证中断后可以从已提交页继续。"""
    # 调用转换方法过滤尚未收盘的K线，并生成数据库字段字典。
    rows, skipped_open = _build_bar_rows(
        frame,
        instrument_id=instrument_id,
        interval=interval,
        now=now,
    )
    for attempt in range(DEADLOCK_RETRIES):
        try:
            # 调用批量upsert，同一品种、周期和开盘时间不会重复插入。
            stored = _upsert_bars(session, rows)
            # 每一页独立提交；网络中断后已经完成的页面不会丢失。
            session.commit()
            return stored, skipped_open
        except OperationalError as error:
            if not _is_retryable_lock_error(error) or attempt == DEADLOCK_RETRIES - 1:
                raise
            # 死锁时 MySQL 已经回滚了该事务，必须先回滚会话才能重发。
            session.rollback()
            time.sleep(DEADLOCK_BACKOFF_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")


def import_gate_history(
    client: GateTestnetClient,
    *,
    engine: Engine | None = None,
    contracts: tuple[str, ...] = DEFAULT_CONTRACTS,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    limit: int = 1000,
    history_days: int | None = None,
    venue: str = GATE_TESTNET_VENUE,
) -> list[ImportResult]:
    """导入最近K线，并可从数据库最早时间向前分批回溯指定天数。"""
    invalid_intervals = set(intervals) - SUPPORTED_INTERVALS
    if invalid_intervals:
        raise ValueError(f"unsupported intervals: {sorted(invalid_intervals)}")
    if not 1 <= limit <= 2000:
        raise ValueError("history import limit must be in [1, 2000]")
    if history_days is not None and not 1 <= history_days <= 3650:
        raise ValueError("history_days must be in [1, 3650]")

    engine = engine or build_engine()
    run_now = datetime.now(UTC)
    target_start = run_now - timedelta(days=history_days) if history_days is not None else None
    results: list[ImportResult] = []
    with Session(engine) as session:
        for contract in contracts:
            # 先查询合约状态；下架中的品种不应继续采集或进入策略。
            contract_info = client.get_contract(contract)
            if contract_info.in_delisting:
                raise RuntimeError(f"Gate contract is delisting: {contract}")

            instrument = _ensure_instrument(session, contract, venue=venue)
            instrument_id = instrument.id
            session.commit()
            for interval in intervals:
                total_fetched = 0
                total_stored = 0
                total_skipped_open = 0
                pages = 0
                history_limited = False
                # 先调用最近K线接口，刷新最新数据并确保新建数据库也具备向前回溯的游标。
                frame = client.get_candlesticks(contract, interval, limit=limit)
                stored, skipped_open = _store_frame(
                    session,
                    frame,
                    instrument_id=instrument_id,
                    interval=interval,
                    now=run_now,
                )
                total_fetched += len(frame)
                total_stored += stored
                total_skipped_open += skipped_open
                pages += 1

                if target_start is not None:
                    duration = INTERVAL_DURATION[interval]
                    retention_start = run_now - duration * (GATE_RECENT_POINT_LIMIT - 2)
                    effective_target_start = max(target_start, retention_start)
                    history_limited = target_start < retention_start
                    # 调用数据库游标查询；重跑时从已保存的最早K线继续向前，不重复下载全部范围。
                    earliest_time = _earliest_bar_time(session, instrument_id, interval)
                    if earliest_time is None:
                        raise RuntimeError(f"no Gate candles returned for {contract} {interval}")
                    cursor_end = earliest_time.replace(tzinfo=UTC) - duration

                    while cursor_end >= effective_target_start:
                        # 每页最多覆盖limit个时间点，符合Gate单次最多2000点的官方约束。
                        page_start = max(
                            effective_target_start - duration,
                            cursor_end - duration * (limit - 1),
                        )
                        # 调用范围K线接口时只发送from/to，客户端不会同时发送冲突的limit参数。
                        page = client.get_candlesticks(
                            contract,
                            interval,
                            limit=limit,
                            start_time=page_start,
                            end_time=cursor_end,
                        )
                        pages += 1
                        total_fetched += len(page)
                        if page.empty:
                            # 测试网在合约上线之前没有行情；空页表示已到可用历史数据边界。
                            break
                        page_stored, page_skipped = _store_frame(
                            session,
                            page,
                            instrument_id=instrument_id,
                            interval=interval,
                            now=run_now,
                        )
                        total_stored += page_stored
                        total_skipped_open += page_skipped
                        oldest_page_time = page.index.min().to_pydatetime()
                        next_cursor_end = oldest_page_time - duration
                        if next_cursor_end >= cursor_end:
                            raise RuntimeError(
                                f"Gate history cursor did not move for {contract} {interval}"
                            )
                        cursor_end = next_cursor_end

                results.append(
                    ImportResult(
                        contract=contract,
                        interval=interval,
                        fetched=total_fetched,
                        stored=total_stored,
                        skipped_open=total_skipped_open,
                        pages=pages,
                        history_limited=history_limited,
                    )
                )
    return results
