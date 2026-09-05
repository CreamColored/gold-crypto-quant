"""下载币安U本位永续实盘公共K线并按独立venue幂等写入MySQL。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.exchanges.binance import BinancePublicClient
from gold_crypto_quant.market_data.gate_history import (
    INTERVAL_DURATION,
    _store_frame,
)
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument

BINANCE_LIVE_VENUE = "BINANCE_LIVE_PUBLIC"
INTERNAL_TO_BINANCE = {"BTC_USDT": "BTCUSDT", "ETH_USDT": "ETHUSDT", "XAU_USDT": "XAUUSDT"}


@dataclass(frozen=True, slots=True)
class BinanceImportResult:
    """单个品种周期的公共K线刷新统计。"""

    contract: str
    interval: str
    fetched: int
    stored: int
    skipped_open: int
    pages: int


def _ensure_instrument(session: Session, internal_symbol: str) -> Instrument:
    """按统一内部代码登记币安合约，策略层无需理解BTCUSDT命名差异。"""
    base_asset, quote_asset = internal_symbol.split("_", maxsplit=1)
    statement = mysql_insert(Instrument).values(
        venue=BINANCE_LIVE_VENUE,
        symbol=internal_symbol,
        asset_class="CRYPTO_FUTURES",
        base_asset=base_asset,
        quote_asset=quote_asset,
        is_active=True,
    )
    session.execute(statement.on_duplicate_key_update(is_active=True))
    session.flush()
    return session.execute(
        select(Instrument).where(
            Instrument.venue == BINANCE_LIVE_VENUE,
            Instrument.symbol == internal_symbol,
        )
    ).scalar_one()


def import_binance_history(
    client: BinancePublicClient,
    *,
    engine: Engine | None = None,
    contracts: tuple[str, ...] = ("BTC_USDT", "ETH_USDT"),
    intervals: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h"),
    limit: int = 500,
    history_days: int | None = None,
) -> list[BinanceImportResult]:
    """刷新最近公开K线，并可分页向前补齐历史；不存在密钥或下单路径。"""
    unknown = set(contracts).difference(INTERNAL_TO_BINANCE)
    if unknown:
        raise ValueError(f"unsupported Binance internal symbols: {sorted(unknown)}")
    invalid_intervals = set(intervals).difference(INTERVAL_DURATION)
    if invalid_intervals:
        raise ValueError(f"unsupported intervals: {sorted(invalid_intervals)}")
    if not 1 <= limit <= 1500:
        raise ValueError("Binance history import limit must be in [1, 1500]")
    if history_days is not None and not 1 <= history_days <= 3650:
        raise ValueError("history_days must be in [1, 3650]")
    engine = engine or build_engine()
    run_now = datetime.now(UTC)
    target_start = run_now - timedelta(days=history_days) if history_days is not None else None
    results: list[BinanceImportResult] = []
    with Session(engine) as session:
        for contract in contracts:
            exchange_symbol = INTERNAL_TO_BINANCE[contract]
            client.get_exchange_symbol(exchange_symbol)
            instrument = _ensure_instrument(session, contract)
            session.commit()
            for interval in intervals:
                total_fetched = 0
                total_stored = 0
                total_skipped_open = 0
                pages = 0
                if target_start is None:
                    frame = client.get_candlesticks(exchange_symbol, interval, limit=limit)
                    # 调用统一存储转换，保证两家交易所采用相同的收线过滤与数据库精度。
                    stored, skipped_open = _store_frame(
                        session,
                        frame,
                        instrument_id=instrument.id,
                        interval=interval,
                        now=run_now,
                    )
                    total_fetched = len(frame)
                    total_stored = stored
                    total_skipped_open = skipped_open
                    pages = 1
                else:
                    # 整个请求窗口都重新分页校准，而不是只从数据库最早一根继续向前。
                    # 后者无法修补旧数据中间的断层；幂等upsert使全窗口重跑仍然安全。
                    duration = INTERVAL_DURATION[interval]
                    cursor_end = run_now
                    while cursor_end >= target_start:
                        # 同时限制页首与页尾，最后一页不会越过用户请求的历史起点。
                        page_start = max(
                            target_start,
                            cursor_end - duration * (limit - 1),
                        )
                        page = client.get_candlesticks(
                            exchange_symbol,
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
                            instrument_id=instrument.id,
                            interval=interval,
                            now=run_now,
                        )
                        total_stored += page_stored
                        total_skipped_open += page_skipped
                        oldest_page_time = page.index.min().to_pydatetime()
                        next_cursor_end = oldest_page_time - duration
                        if next_cursor_end >= cursor_end:
                            raise RuntimeError(
                                f"Binance history cursor did not move for {contract} {interval}"
                            )
                        cursor_end = next_cursor_end

                results.append(
                    BinanceImportResult(
                        contract=contract,
                        interval=interval,
                        fetched=total_fetched,
                        stored=total_stored,
                        skipped_open=total_skipped_open,
                        pages=pages,
                    )
                )
    return results
