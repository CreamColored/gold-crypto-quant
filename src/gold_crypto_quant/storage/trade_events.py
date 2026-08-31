"""读取尚未通知的本地模拟成交事件。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, Order, Trade


@dataclass(frozen=True, slots=True)
class PaperTradeEvent:
    """一笔可发送邮件的本地模拟成交摘要。"""

    trade_id: int
    symbol: str
    order_side: str
    position_side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal | None
    exit_reason: str | None
    executed_at: datetime


def latest_paper_trade_id(
    *, paper_venue: str = "GATE_TESTNET_PAPER", engine: Engine | None = None
) -> int:
    """返回当前最大成交ID；服务启动时用于避免重发旧成交邮件。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        value = session.scalar(
            select(func.coalesce(func.max(Trade.id), 0)).where(Trade.venue == paper_venue)
        )
        return int(value or 0)


def read_paper_trade_events(
    after_trade_id: int,
    *,
    paper_venue: str = "GATE_TESTNET_PAPER",
    engine: Engine | None = None,
) -> tuple[PaperTradeEvent, ...]:
    """按ID升序读取指定游标之后的全部成交，供每笔成交逐封通知。"""
    if after_trade_id < 0:
        raise ValueError("after trade id cannot be negative")
    engine = engine or build_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(Trade, Order, Instrument.symbol)
            .join(Order, Order.id == Trade.order_id)
            .join(Instrument, Instrument.id == Trade.instrument_id)
            .where(Trade.venue == paper_venue, Trade.id > after_trade_id)
            .order_by(Trade.id)
        ).all()
    events: list[PaperTradeEvent] = []
    for trade, order, symbol in rows:
        raw = order.raw_response if isinstance(order.raw_response, dict) else {}
        events.append(
            PaperTradeEvent(
                trade_id=trade.id,
                symbol=symbol,
                order_side=order.side,
                position_side=order.position_side,
                quantity=trade.quantity,
                price=trade.price,
                fee=trade.fee,
                realized_pnl=trade.realized_pnl,
                exit_reason=(str(raw["exit_reason"]) if raw.get("exit_reason") else None),
                executed_at=trade.executed_at.replace(tzinfo=UTC),
            )
        )
    return tuple(events)
