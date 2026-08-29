"""下载OANDA Practice黄金历史K线并幂等写入MySQL。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.exchanges.oanda import SUPPORTED_INTERVALS, OandaPracticeClient
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, MarketBar

OANDA_PRACTICE_VENUE = "OANDA_PRACTICE"
DEFAULT_INSTRUMENT = "XAU_USD"
DEFAULT_INTERVALS = ("5m", "15m", "30m", "1h")
INTERVAL_DURATION = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
}


@dataclass(frozen=True, slots=True)
class OandaImportResult:
    """一个OANDA品种周期的导入统计。"""

    instrument: str
    interval: str
    fetched: int
    stored: int
    skipped_open: int
    pages: int


def _split_instrument(instrument: str) -> tuple[str, str]:
    """把OANDA品种代码拆成基础资产和计价资产。"""
    try:
        base_asset, quote_asset = instrument.split("_", maxsplit=1)
    except ValueError as exc:
        raise ValueError(f"invalid OANDA instrument name: {instrument}") from exc
    if not base_asset or not quote_asset:
        raise ValueError(f"invalid OANDA instrument name: {instrument}")
    return base_asset, quote_asset


def _ensure_instrument(session: Session, instrument: str) -> Instrument:
    """幂等登记OANDA Practice黄金品种。"""
    base_asset, quote_asset = _split_instrument(instrument)
    statement = mysql_insert(Instrument).values(
        venue=OANDA_PRACTICE_VENUE,
        symbol=instrument,
        asset_class="METAL_CFD",
        base_asset=base_asset,
        quote_asset=quote_asset,
        is_active=True,
    )
    # 调用MySQL唯一键更新，重复导入只重新启用品种，不产生重复主数据。
    session.execute(statement.on_duplicate_key_update(is_active=True))
    session.flush()
    # 调用品种唯一键查询，兼容首次插入和重复刷新两种情况。
    return session.execute(
        select(Instrument).where(
            Instrument.venue == OANDA_PRACTICE_VENUE,
            Instrument.symbol == instrument,
        )
    ).scalar_one()


def _build_bar_rows(
    frame: pd.DataFrame,
    *,
    instrument_id: int,
    interval: str,
    now: datetime | None = None,
) -> tuple[list[dict[str, object]], int]:
    """转换OANDA中间价K线，并拒绝未完成或尚未到收盘时间的数据。"""
    if interval not in INTERVAL_DURATION:
        raise ValueError(f"unsupported interval: {interval}")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    duration = INTERVAL_DURATION[interval]
    rows: list[dict[str, object]] = []
    skipped_open = 0
    for open_time, candle in frame.iterrows():
        aware_open_time = open_time.to_pydatetime()
        close_time = aware_open_time + duration
        if not bool(candle["complete"]) or close_time > now:
            skipped_open += 1
            continue
        rows.append(
            {
                "instrument_id": instrument_id,
                "interval_code": interval,
                "open_time": aware_open_time.astimezone(UTC).replace(tzinfo=None),
                "close_time": close_time.astimezone(UTC).replace(tzinfo=None),
                "open_price": Decimal(str(candle["open"])),
                "high_price": Decimal(str(candle["high"])),
                "low_price": Decimal(str(candle["low"])),
                "close_price": Decimal(str(candle["close"])),
                # OANDA volume是该K线内生成的价格数量，不是黄金实际成交量。
                "volume": Decimal(str(candle["volume"])),
                "quote_volume": None,
                "trade_count": None,
                "is_closed": True,
            }
        )
    return rows, skipped_open


def _store_frame(
    session: Session,
    frame: pd.DataFrame,
    *,
    instrument_id: int,
    interval: str,
    now: datetime,
) -> tuple[int, int]:
    """转换并幂等提交一页OANDA K线。"""
    rows, skipped_open = _build_bar_rows(
        frame, instrument_id=instrument_id, interval=interval, now=now
    )
    if rows:
        statement = mysql_insert(MarketBar).values(rows)
        # 调用批量upsert，OANDA修正历史蜡烛时刷新OHLC而不重复新增。
        session.execute(
            statement.on_duplicate_key_update(
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
        )
    session.commit()
    return len(rows), skipped_open


def import_oanda_history(
    client: OandaPracticeClient,
    *,
    engine: Engine | None = None,
    instrument: str = DEFAULT_INSTRUMENT,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    limit: int = 1000,
    history_days: int | None = None,
) -> list[OandaImportResult]:
    """导入黄金最近K线，并可从数据库游标向前补齐指定天数。"""
    invalid_intervals = set(intervals) - SUPPORTED_INTERVALS
    if invalid_intervals:
        raise ValueError(f"unsupported intervals: {sorted(invalid_intervals)}")
    if not 1 <= limit <= 5000:
        raise ValueError("OANDA history import limit must be in [1, 5000]")
    if history_days is not None and not 1 <= history_days <= 3650:
        raise ValueError("history_days must be in [1, 3650]")

    engine = engine or build_engine()
    run_now = datetime.now(UTC)
    target_start = run_now - timedelta(days=history_days) if history_days else None
    results: list[OandaImportResult] = []
    with Session(engine) as session:
        database_instrument = _ensure_instrument(session, instrument)
        instrument_id = database_instrument.id
        session.commit()
        for interval in intervals:
            total_fetched = 0
            total_stored = 0
            total_skipped_open = 0
            pages = 0
            # 调用最近蜡烛接口，先补齐最新数据并为新数据库建立历史游标。
            frame = client.get_candlesticks(instrument, interval, limit=limit)
            stored, skipped = _store_frame(
                session,
                frame,
                instrument_id=instrument_id,
                interval=interval,
                now=run_now,
            )
            total_fetched += len(frame)
            total_stored += stored
            total_skipped_open += skipped
            pages += 1

            if target_start is not None:
                duration = INTERVAL_DURATION[interval]
                earliest = session.scalar(
                    select(func.min(MarketBar.open_time)).where(
                        MarketBar.instrument_id == instrument_id,
                        MarketBar.interval_code == interval,
                    )
                )
                if earliest is None:
                    raise RuntimeError(f"no OANDA candles returned for {instrument} {interval}")
                cursor_end = earliest.replace(tzinfo=UTC)
                while cursor_end > target_start:
                    # 每页范围最多5000个周期，符合OANDA官方蜡烛数量上限。
                    page_start = max(target_start, cursor_end - duration * limit)
                    page = client.get_candlesticks(
                        instrument,
                        interval,
                        limit=limit,
                        start_time=page_start,
                        end_time=cursor_end,
                    )
                    pages += 1
                    total_fetched += len(page)
                    if page.empty:
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
                    next_cursor_end = page.index.min().to_pydatetime()
                    if next_cursor_end >= cursor_end:
                        raise RuntimeError(
                            f"OANDA history cursor did not move for {instrument} {interval}"
                        )
                    cursor_end = next_cursor_end

            results.append(
                OandaImportResult(
                    instrument=instrument,
                    interval=interval,
                    fetched=total_fetched,
                    stored=total_stored,
                    skipped_open=total_skipped_open,
                    pages=pages,
                )
            )
    return results
