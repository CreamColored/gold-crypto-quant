"""通过准入门禁和仓位限制预留本地测试订单。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.execution.orders import OrderIntent, build_client_order_id
from gold_crypto_quant.execution.risk_limits import PositionLimitPolicy, validate_position_limits
from gold_crypto_quant.execution.states import OrderStatus
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.market_health import require_market_health_for_order
from gold_crypto_quant.storage.models import (
    Order,
    Position,
    RuntimeRiskState,
    StrategyQualification,
)

ACTIVE_ENTRY_STATUSES = (
    OrderStatus.CREATED.value,
    OrderStatus.SUBMITTING.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.UNKNOWN.value,
)


@dataclass(frozen=True, slots=True)
class ReservedPaperOrder:
    """本地数据库中已完成幂等预留、但从未发送到接口的订单。"""

    order_id: int
    client_order_id: str
    created: bool


def reserve_paper_order(
    intent: OrderIntent,
    *,
    engine: Engine | None = None,
    policy: PositionLimitPolicy | None = None,
    now: datetime | None = None,
    risk_venue: str = "GATE_TESTNET",
    paper_venue: str = "GATE_TESTNET_PAPER",
    quantity_unit: str = "GATE_CONTRACTS",
) -> ReservedPaperOrder:
    """通过准入与仓位门禁后幂等创建CREATED状态本地订单。"""
    engine = engine or build_engine()
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not paper_venue or not quantity_unit:
        raise ValueError("paper venue and quantity unit are required")
    client_order_id = build_client_order_id(intent)

    with Session(engine) as session:
        # 调用准入哈希查询并锁定同一审批内容；拒绝记录绝不能创建开仓订单。
        qualification = session.execute(
            select(StrategyQualification).where(
                StrategyQualification.evaluation_hash == intent.qualification_hash
            )
        ).scalar_one_or_none()
        if qualification is None:
            raise PermissionError("strategy qualification does not exist")
        if qualification.decision != "APPROVED":
            raise PermissionError("strategy qualification is not approved")
        if (
            qualification.instrument_id != intent.instrument_id
            or qualification.interval_code != intent.interval
        ):
            raise PermissionError("qualification does not match order instrument and interval")
        # 风控状态缺失时失败关闭；只有显式NORMAL状态允许继续预留订单。
        runtime_risk = session.execute(
            select(RuntimeRiskState).where(RuntimeRiskState.venue == risk_venue)
        ).scalar_one_or_none()
        if runtime_risk is None:
            raise PermissionError("runtime risk state is missing")
        if runtime_risk.state != "NORMAL":
            raise PermissionError(f"runtime risk state blocks orders: {runtime_risk.state}")
        # 调用行情门禁重新计算心跳时效，数据库里旧的HEALTHY状态不能被无限期复用。
        require_market_health_for_order(
            session,
            instrument_id=intent.instrument_id,
            interval=intent.interval,
            signal_time=intent.bar_time,
            now=now,
        )

        existing_order = session.execute(
            select(Order).where(Order.client_order_id == client_order_id)
        ).scalar_one_or_none()
        if existing_order is not None:
            # 重试必须与原订单关键字段完全一致，禁止复用幂等编号偷偷改变数量或方向。
            if (
                existing_order.instrument_id != intent.instrument_id
                or existing_order.side != intent.side
                or existing_order.position_side != intent.position_side
                or existing_order.quantity != intent.quantity
                or existing_order.order_type != intent.order_type
                or existing_order.price != intent.price
                or existing_order.stop_price != intent.stop_price
                or existing_order.reduce_only != intent.reduce_only
                or not isinstance(existing_order.raw_response, dict)
                or existing_order.raw_response.get("leverage") != intent.leverage
                or existing_order.raw_response.get("paper_venue", "GATE_TESTNET_PAPER")
                != paper_venue
                or existing_order.raw_response.get("quantity_unit", "GATE_CONTRACTS")
                != quantity_unit
                or existing_order.raw_response.get("metadata") != (intent.metadata or {})
            ):
                raise RuntimeError("existing idempotent order does not match retry intent")
            return ReservedPaperOrder(
                order_id=existing_order.id,
                client_order_id=client_order_id,
                created=False,
            )

        # 调用持仓查询，任何方向存在非零仓位都视为该品种已占用，禁止补仓或反向新开。
        open_instrument_ids = set(
            session.scalars(select(Position.instrument_id).where(Position.quantity != 0)).all()
        )
        # 调用活动订单查询，断线后的UNKNOWN也继续占用名额，防止状态不明时重复下单。
        pending_entry_instrument_ids = set(
            session.scalars(
                select(Order.instrument_id).where(
                    Order.status.in_(ACTIVE_ENTRY_STATUSES),
                    Order.reduce_only.is_(False),
                )
            ).all()
        )
        # 调用纯风控方法验证禁止补仓、待处理重复单、杠杆和总持仓数量。
        validate_position_limits(
            intent,
            open_instrument_ids=open_instrument_ids,
            pending_entry_instrument_ids=pending_entry_instrument_ids,
            policy=policy,
        )

        statement = mysql_insert(Order).values(
            client_order_id=client_order_id,
            venue_order_id=None,
            strategy_run_id=intent.strategy_run_id,
            instrument_id=intent.instrument_id,
            side=intent.side,
            position_side=intent.position_side,
            order_type=intent.order_type,
            status=OrderStatus.CREATED.value,
            quantity=intent.quantity,
            price=intent.price,
            stop_price=intent.stop_price,
            reduce_only=intent.reduce_only,
            submitted_at=now.astimezone(UTC).replace(tzinfo=None),
            raw_response={
                "mode": "LOCAL_PAPER_RESERVATION_ONLY",
                "qualification_hash": intent.qualification_hash,
                "leverage": intent.leverage,
                "paper_venue": paper_venue,
                "quantity_unit": quantity_unit,
                "live_trading": False,
                "interval": intent.interval,
                "signal_bar_time": intent.bar_time.isoformat(),
                "metadata": intent.metadata or {},
            },
        )
        # 唯一client_order_id发生冲突时保持原记录不变，重试绝不会创建第二张订单。
        session.execute(
            statement.on_duplicate_key_update(client_order_id=statement.inserted.client_order_id)
        )
        session.commit()
        order_id = session.execute(
            select(Order.id).where(Order.client_order_id == client_order_id)
        ).scalar_one()
        return ReservedPaperOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            created=True,
        )
