"""本地模拟订单成交、持仓更新和保护止损执行。"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.execution.paper_fills import (
    calculate_adverse_fill_price,
    calculate_position_pnl,
    find_first_stop_trigger,
)
from gold_crypto_quant.execution.states import OrderStatus
from gold_crypto_quant.market_data.gate_history import INTERVAL_DURATION
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.models import (
    Instrument,
    Order,
    PaperPositionControl,
    Position,
    Trade,
)
from gold_crypto_quant.storage.paper_account import refresh_paper_account
from gold_crypto_quant.strategy.live_signals import NextBarSignalDecision, SignalAction

PAPER_VENUE = "GATE_TESTNET_PAPER"


@dataclass(frozen=True, slots=True)
class PaperFillResult:
    """本地模拟成交的幂等结果。"""

    order_id: int
    trade_id: int
    position_id: int
    created: bool
    fill_price: Decimal
    realized_pnl: Decimal | None


@dataclass(frozen=True, slots=True)
class PaperPositionMonitorResult:
    """一次持仓监控的盯市或止损结果。"""

    position_id: int
    status: str
    exit_fill: PaperFillResult | None


def _paper_trade_key(client_order_id: str, event: str) -> str:
    """根据本地订单和事件生成确定性模拟成交编号。"""
    digest = hashlib.sha256(f"{client_order_id}|{event}".encode()).hexdigest()[:40]
    return f"paper-{digest}"


def _required_metadata(order: Order) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """读取订单预留时冻结的合约和成交参数。"""
    raw = order.raw_response
    metadata = raw.get("metadata") if isinstance(raw, dict) else None
    if not isinstance(metadata, dict):
        raise RuntimeError("paper order metadata is missing")
    try:
        return (
            Decimal(str(metadata["contract_multiplier"])),
            Decimal(str(metadata["mark_price_at_reservation"])),
            Decimal(str(metadata["price_tick"])),
            Decimal(str(metadata["taker_fee_rate"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("paper order metadata is invalid") from error


def fill_created_paper_order(
    order_id: int,
    *,
    now: datetime | None = None,
    engine: Engine | None = None,
) -> PaperFillResult:
    """把一个CREATED本地订单原子转换为成交和非零持仓。"""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("paper fill time must be timezone-aware")
    engine = engine or build_engine()
    database_now = now.astimezone(UTC).replace(tzinfo=None)
    with Session(engine) as session, session.begin():
        # 调用行锁确保两个执行进程不能同时成交同一张本地订单。
        order = session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
        ).scalar_one()
        raw_response = order.raw_response if isinstance(order.raw_response, dict) else {}
        paper_venue = str(raw_response.get("paper_venue", PAPER_VENUE))
        trade_key = _paper_trade_key(order.client_order_id, "ENTRY_FILL")
        existing_trade = session.execute(
            select(Trade).where(
                Trade.venue == paper_venue,
                Trade.venue_trade_id == trade_key,
            )
        ).scalar_one_or_none()
        if existing_trade is not None:
            position_id = session.execute(
                select(Position.id).where(
                    Position.venue == paper_venue,
                    Position.instrument_id == order.instrument_id,
                    Position.position_side == order.position_side,
                )
            ).scalar_one()
            return PaperFillResult(
                order_id=order.id,
                trade_id=existing_trade.id,
                position_id=position_id,
                created=False,
                fill_price=existing_trade.price,
                realized_pnl=existing_trade.realized_pnl,
            )
        if order.status != OrderStatus.CREATED.value:
            raise PermissionError(f"paper order is not fillable: {order.status}")
        if order.reduce_only:
            raise PermissionError("entry paper fill cannot be reduce-only")
        if not isinstance(order.raw_response, dict) or order.raw_response.get("mode") != (
            "LOCAL_PAPER_RESERVATION_ONLY"
        ):
            raise PermissionError("order is not a local paper reservation")
        multiplier, reference_price, price_tick, fee_rate = _required_metadata(order)
        slippage = Decimal(str(order.raw_response["metadata"].get("slippage_rate", "0.0002")))
        # 调用不利滑点方法模拟市价成交，买单向上、卖单向下取价格步长。
        fill_price = calculate_adverse_fill_price(
            reference_price,
            order_side=order.side,
            price_tick=price_tick,
            slippage_rate=slippage,
        )
        base_quantity = order.quantity * multiplier
        fee = abs(base_quantity * fill_price * fee_rate)
        trade = Trade(
            order_id=order.id,
            instrument_id=order.instrument_id,
            venue=paper_venue,
            venue_trade_id=trade_key,
            price=fill_price,
            quantity=order.quantity,
            fee=fee,
            fee_asset=str(order.raw_response["metadata"].get("fee_asset", "USDT")),
            realized_pnl=None,
            executed_at=database_now,
            raw_response={
                "mode": "LOCAL_PAPER_FILL",
                "contract_multiplier": str(multiplier),
                "base_quantity": str(base_quantity),
                "live_trading": False,
            },
        )
        session.add(trade)
        position = session.execute(
            select(Position)
            .where(
                Position.venue == paper_venue,
                Position.instrument_id == order.instrument_id,
                Position.position_side == order.position_side,
            )
            .with_for_update()
        ).scalar_one_or_none()
        leverage = int(order.raw_response["leverage"])
        if position is None:
            position = Position(
                venue=paper_venue,
                instrument_id=order.instrument_id,
                position_side=order.position_side,
                quantity=order.quantity,
                entry_price=fill_price,
                mark_price=fill_price,
                leverage=leverage,
                liquidation_price=None,
                unrealized_pnl=Decimal("0"),
            )
            session.add(position)
            session.flush()
        else:
            if position.quantity != 0:
                raise PermissionError("pyramiding is forbidden during paper fill")
            position.quantity = order.quantity
            position.entry_price = fill_price
            position.mark_price = fill_price
            position.leverage = leverage
            position.liquidation_price = None
            position.unrealized_pnl = Decimal("0")
        if order.strategy_run_id is None or order.stop_price is None:
            raise RuntimeError("paper entry order lacks strategy run or protective stop")
        interval = str(order.raw_response.get("interval"))
        if interval not in INTERVAL_DURATION:
            raise RuntimeError("paper entry order interval is invalid")
        signal_bar_time = datetime.fromisoformat(str(order.raw_response["signal_bar_time"]))
        previous_bar_time = signal_bar_time - INTERVAL_DURATION[interval]
        control_statement = mysql_insert(PaperPositionControl).values(
            position_id=position.id,
            entry_order_id=order.id,
            strategy_run_id=order.strategy_run_id,
            interval_code=interval,
            contract_multiplier=multiplier,
            protective_stop_price=order.stop_price,
            opened_at=database_now,
            last_evaluated_bar_time=previous_bar_time.astimezone(UTC).replace(tzinfo=None),
            closed_at=None,
            exit_reason=None,
        )
        # 调用持仓ID幂等更新；同方向仓位重新开启时重置控制信息但保留成交历史。
        session.execute(
            control_statement.on_duplicate_key_update(
                entry_order_id=control_statement.inserted.entry_order_id,
                strategy_run_id=control_statement.inserted.strategy_run_id,
                interval_code=control_statement.inserted.interval_code,
                contract_multiplier=control_statement.inserted.contract_multiplier,
                protective_stop_price=control_statement.inserted.protective_stop_price,
                opened_at=control_statement.inserted.opened_at,
                last_evaluated_bar_time=control_statement.inserted.last_evaluated_bar_time,
                closed_at=None,
                exit_reason=None,
            )
        )
        order.status = OrderStatus.FILLED.value
        order.raw_response = {
            **order.raw_response,
            "paper_fill_price": str(fill_price),
            "paper_filled_at": now.isoformat(),
        }
        session.flush()
        return PaperFillResult(
            order_id=order.id,
            trade_id=trade.id,
            position_id=position.id,
            created=True,
            fill_price=fill_price,
            realized_pnl=None,
        )


def close_paper_position(
    position_id: int,
    *,
    reference_price: Decimal,
    executed_at: datetime,
    exit_reason: str,
    engine: Engine | None = None,
) -> PaperFillResult:
    """创建只减仓本地平仓订单并原子写入成交、盈亏和零仓位。"""
    if executed_at.tzinfo is None:
        raise ValueError("paper close time must be timezone-aware")
    if not exit_reason:
        raise ValueError("paper close reason is required")
    engine = engine or build_engine()
    database_time = executed_at.astimezone(UTC).replace(tzinfo=None)
    with Session(engine) as session, session.begin():
        position = session.execute(
            select(Position).where(Position.id == position_id).with_for_update()
        ).scalar_one()
        control = session.execute(
            select(PaperPositionControl)
            .where(PaperPositionControl.position_id == position_id)
            .with_for_update()
        ).scalar_one()
        entry_order = session.execute(
            select(Order).where(Order.id == control.entry_order_id)
        ).scalar_one()
        close_identity = (
            f"{entry_order.client_order_id}|{exit_reason}|{executed_at.isoformat()}"
        )
        close_digest = hashlib.sha256(close_identity.encode()).hexdigest()[:40]
        client_order_id = f"gcq-close-{close_digest}"
        existing_order = session.execute(
            select(Order).where(Order.client_order_id == client_order_id)
        ).scalar_one_or_none()
        if existing_order is not None:
            existing_trade = session.execute(
                select(Trade).where(Trade.order_id == existing_order.id)
            ).scalar_one()
            return PaperFillResult(
                order_id=existing_order.id,
                trade_id=existing_trade.id,
                position_id=position.id,
                created=False,
                fill_price=existing_trade.price,
                realized_pnl=existing_trade.realized_pnl,
            )
        if position.quantity <= 0 or control.closed_at is not None:
            raise PermissionError("paper position is already closed")
        multiplier, _reserved_mark, price_tick, fee_rate = _required_metadata(entry_order)
        metadata = entry_order.raw_response["metadata"]
        paper_venue = position.venue
        fee_asset = str(metadata.get("fee_asset", "USDT"))
        slippage = Decimal(str(metadata.get("slippage_rate", "0.0002")))
        close_side = "SELL" if position.position_side == "LONG" else "BUY"
        fill_price = calculate_adverse_fill_price(
            reference_price,
            order_side=close_side,
            price_tick=price_tick,
            slippage_rate=slippage,
        )
        base_quantity = position.quantity * multiplier
        exit_fee = abs(base_quantity * fill_price * fee_rate)
        entry_fee = session.execute(
            select(Trade.fee).where(Trade.order_id == entry_order.id)
        ).scalar_one()
        gross_pnl = calculate_position_pnl(
            position_side=position.position_side,
            entry_price=position.entry_price,
            exit_price=fill_price,
            contract_quantity=position.quantity,
            contract_multiplier=multiplier,
        )
        net_pnl = gross_pnl - entry_fee - exit_fee
        close_order = Order(
            client_order_id=client_order_id,
            venue_order_id=None,
            strategy_run_id=control.strategy_run_id,
            instrument_id=position.instrument_id,
            side=close_side,
            position_side=position.position_side,
            order_type="MARKET",
            status=OrderStatus.FILLED.value,
            quantity=position.quantity,
            price=None,
            stop_price=(control.protective_stop_price if exit_reason == "ATR_STOP" else None),
            reduce_only=True,
            submitted_at=database_time,
            raw_response={
                "mode": "LOCAL_PAPER_CLOSE",
                "exit_reason": exit_reason,
                "live_trading": False,
            },
        )
        session.add(close_order)
        session.flush()
        close_trade = Trade(
            order_id=close_order.id,
            instrument_id=position.instrument_id,
            venue=paper_venue,
            venue_trade_id=_paper_trade_key(client_order_id, "EXIT_FILL"),
            price=fill_price,
            quantity=position.quantity,
            fee=exit_fee,
            fee_asset=fee_asset,
            # 账本统一用毛盈亏减全部Trade.fee，避免入场手续费被重复扣除。
            realized_pnl=gross_pnl,
            executed_at=database_time,
            raw_response={
                "mode": "LOCAL_PAPER_FILL",
                "exit_reason": exit_reason,
                "gross_pnl": str(gross_pnl),
                "entry_fee": str(entry_fee),
                "exit_fee": str(exit_fee),
                "net_pnl": str(net_pnl),
                "live_trading": False,
            },
        )
        session.add(close_trade)
        position.quantity = Decimal("0")
        position.mark_price = fill_price
        position.unrealized_pnl = Decimal("0")
        control.closed_at = database_time
        control.exit_reason = exit_reason
        control.last_evaluated_bar_time = database_time
        session.flush()
        return PaperFillResult(
            order_id=close_order.id,
            trade_id=close_trade.id,
            position_id=position.id,
            created=True,
            fill_price=fill_price,
            realized_pnl=gross_pnl,
        )


def monitor_paper_positions(
    *,
    bar_limit: int = 500,
    paper_venue: str = PAPER_VENUE,
    source_venue: str = "GATE_TESTNET",
    engine: Engine | None = None,
) -> list[PaperPositionMonitorResult]:
    """盯市全部非零模拟仓位，并在首根触发K线执行ATR止损。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        rows = session.execute(
            select(Position, PaperPositionControl, Instrument.symbol)
            .join(PaperPositionControl, PaperPositionControl.position_id == Position.id)
            .join(Instrument, Instrument.id == Position.instrument_id)
            .where(
                Position.venue == paper_venue,
                Position.quantity > 0,
                PaperPositionControl.closed_at.is_(None),
            )
        ).all()
    results: list[PaperPositionMonitorResult] = []
    for position, control, symbol in rows:
        # 调用连续性校验后的最近K线读取，缺口会停止本仓位自动处理并交给上层报警。
        bars = load_market_bars(
            symbol,
            control.interval_code,
            venue=source_venue,
            engine=engine,
            limit=bar_limit,
        )
        last_evaluated = (
            control.last_evaluated_bar_time.replace(tzinfo=UTC)
            if control.last_evaluated_bar_time is not None
            else None
        )
        duration = INTERVAL_DURATION[control.interval_code]
        if last_evaluated is not None and bars.index[0].to_pydatetime() > (
            last_evaluated + duration
        ):
            raise RuntimeError(f"paper stop history gap for position {position.id}")
        # 调用首个止损触发查找，严格按时间处理停机期间积累的已收盘K线。
        trigger = find_first_stop_trigger(
            bars,
            position_side=position.position_side,
            stop_price=control.protective_stop_price,
            after_bar_time=last_evaluated,
        )
        if trigger is not None:
            fill = close_paper_position(
                position.id,
                reference_price=trigger.reference_price,
                executed_at=trigger.bar_time + duration,
                exit_reason="ATR_STOP",
                engine=engine,
            )
            results.append(
                PaperPositionMonitorResult(
                    position_id=position.id,
                    status="ATR_STOP_FILLED",
                    exit_fill=fill,
                )
            )
            continue
        latest_time = bars.index[-1].to_pydatetime()
        latest_mark = Decimal(str(bars.iloc[-1]["close"]))
        unrealized_pnl = calculate_position_pnl(
            position_side=position.position_side,
            entry_price=position.entry_price,
            exit_price=latest_mark,
            contract_quantity=position.quantity,
            contract_multiplier=control.contract_multiplier,
        )
        with Session(engine) as session, session.begin():
            # 调用行锁后更新盯市价、未实现盈亏和止损检查游标。
            current_position = session.execute(
                select(Position).where(Position.id == position.id).with_for_update()
            ).scalar_one()
            current_control = session.execute(
                select(PaperPositionControl)
                .where(PaperPositionControl.position_id == position.id)
                .with_for_update()
            ).scalar_one()
            if current_position.quantity > 0 and current_control.closed_at is None:
                current_position.mark_price = latest_mark
                current_position.unrealized_pnl = unrealized_pnl
                current_control.last_evaluated_bar_time = latest_time.astimezone(UTC).replace(
                    tzinfo=None
                )
        results.append(
            PaperPositionMonitorResult(
                position_id=position.id,
                status="MARKED_TO_MARKET",
                exit_fill=None,
            )
        )
    # 全部仓位完成止损或盯市后调用一次账本刷新，让浮动盈亏立即进入2%与8%熔断。
    refresh_paper_account(paper_venue=paper_venue, engine=engine)
    return results


def close_position_for_ema_signal(
    *,
    symbol: str,
    interval: str,
    decision: NextBarSignalDecision,
    action: SignalAction,
    paper_venue: str = PAPER_VENUE,
    source_venue: str = "GATE_TESTNET",
    engine: Engine | None = None,
) -> PaperFillResult | None:
    """按EMA退出动作关闭匹配方向的模拟仓位；无持仓时安全跳过。"""
    if action.signal_type != "EXIT":
        raise ValueError("EMA close executor only accepts EXIT actions")
    engine = engine or build_engine()
    with Session(engine) as session:
        position_id = session.execute(
            select(Position.id)
            .join(PaperPositionControl, PaperPositionControl.position_id == Position.id)
            .join(Instrument, Instrument.id == Position.instrument_id)
            .where(
                Position.venue == paper_venue,
                Position.quantity > 0,
                Position.position_side == action.side,
                Instrument.symbol == symbol,
                Instrument.venue == source_venue,
                PaperPositionControl.interval_code == interval,
                PaperPositionControl.closed_at.is_(None),
            )
        ).scalar_one_or_none()
    if position_id is None:
        return None
    # 调用原子平仓方法，以信号确认后的下一根开盘参考价模拟只减仓成交。
    fill = close_paper_position(
        position_id,
        reference_price=decision.signal_price,
        executed_at=decision.bar_time,
        exit_reason="EMA_EXIT",
        engine=engine,
    )
    # EMA平仓成交后调用账本刷新，已实现盈亏和手续费立即进入风控权益。
    refresh_paper_account(
        now=decision.bar_time,
        paper_venue=paper_venue,
        engine=engine,
    )
    return fill
