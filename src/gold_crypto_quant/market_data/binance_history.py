"""下载币安U本位永续实盘公共K线并按独立venue幂等写入MySQL。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.exchanges.binance import BinancePublicClient
from gold_crypto_quant.market_data.gate_history import _store_frame
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument

BINANCE_LIVE_VENUE = "BINANCE_LIVE_PUBLIC"
INTERNAL_TO_BINANCE = {"BTC_USDT": "BTCUSDT", "ETH_USDT": "ETHUSDT"}


@dataclass(frozen=True, slots=True)
class BinanceImportResult:
    """单个品种周期的公共K线刷新统计。"""

    contract: str
    interval: str
    fetched: int
    stored: int
    skipped_open: int


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
) -> list[BinanceImportResult]:
    """刷新最近公开K线；只读公共接口，不存在API密钥或下单路径。"""
    unknown = set(contracts).difference(INTERNAL_TO_BINANCE)
    if unknown:
        raise ValueError(f"unsupported Binance internal symbols: {sorted(unknown)}")
    engine = engine or build_engine()
    run_now = datetime.now(UTC)
    results: list[BinanceImportResult] = []
    with Session(engine) as session:
        for contract in contracts:
            exchange_symbol = INTERNAL_TO_BINANCE[contract]
            client.get_exchange_symbol(exchange_symbol)
            instrument = _ensure_instrument(session, contract)
            session.commit()
            for interval in intervals:
                frame = client.get_candlesticks(exchange_symbol, interval, limit=limit)
                # 调用统一存储转换，保证Gate和币安采用相同的收线过滤与数据库精度。
                stored, skipped_open = _store_frame(
                    session,
                    frame,
                    instrument_id=instrument.id,
                    interval=interval,
                    now=run_now,
                )
                results.append(
                    BinanceImportResult(
                        contract=contract,
                        interval=interval,
                        fetched=len(frame),
                        stored=stored,
                        skipped_open=skipped_open,
                    )
                )
    return results
