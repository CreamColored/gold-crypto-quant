"""只读查询当前执行安全状态。"""

from dataclasses import dataclass

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gold_crypto_quant.execution.states import OrderStatus
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import (
    Instrument,
    MarketDataHealthState,
    Order,
    PaperAccountState,
    PaperPositionControl,
    Position,
    RuntimeRiskState,
    StrategyQualification,
    StrategySignal,
    Trade,
)

ACTIVE_STRATEGY_NAME = "BOLLINGER_RANGE"
ACTIVE_STRATEGY_VERSION = "3.0.0"


@dataclass(frozen=True, slots=True)
class ExecutionSafetyStatus:
    """不包含密钥和余额的本地执行状态摘要。"""

    approved_qualifications: int
    rejected_qualifications: int
    active_orders: int
    open_positions: int
    runtime_risk_state: str
    healthy_market_streams: int
    unhealthy_market_streams: int
    strategy_signals: int
    paper_trades: int
    closed_paper_positions: int
    paper_account_equity: str | None
    gate_runtime_risk_state: str
    oanda_runtime_risk_state: str


def read_execution_safety_status(
    engine: Engine | None = None, *, oanda_enabled: bool = False
) -> ExecutionSafetyStatus:
    """按当前启用场所只读统计准入、活动订单和非零仓位数量。"""
    engine = engine or build_engine()
    active_statuses = (
        OrderStatus.CREATED.value,
        OrderStatus.SUBMITTING.value,
        OrderStatus.SUBMITTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
        OrderStatus.UNKNOWN.value,
    )
    enabled_venues = (
        ("GATE_TESTNET", "OANDA_PRACTICE") if oanda_enabled else ("GATE_TESTNET",)
    )
    with Session(engine) as session:
        # 以下四次scalar查询只读取计数，不加载订单、仓位或审批明细。
        approved = session.scalar(
            select(func.count())
            .select_from(StrategyQualification)
            .join(Instrument, Instrument.id == StrategyQualification.instrument_id)
            .where(
                StrategyQualification.decision == "APPROVED",
                StrategyQualification.strategy_name == ACTIVE_STRATEGY_NAME,
                StrategyQualification.strategy_version == ACTIVE_STRATEGY_VERSION,
                Instrument.venue.in_(enabled_venues),
            )
        )
        rejected = session.scalar(
            select(func.count())
            .select_from(StrategyQualification)
            .join(Instrument, Instrument.id == StrategyQualification.instrument_id)
            .where(
                StrategyQualification.decision == "REJECTED",
                StrategyQualification.strategy_name == ACTIVE_STRATEGY_NAME,
                StrategyQualification.strategy_version == ACTIVE_STRATEGY_VERSION,
                Instrument.venue.in_(enabled_venues),
            )
        )
        active_orders = session.scalar(
            select(func.count())
            .select_from(Order)
            .join(Instrument, Instrument.id == Order.instrument_id)
            .where(
                Order.status.in_(active_statuses),
                Instrument.venue.in_(enabled_venues),
            )
        )
        open_positions = session.scalar(
            select(func.count())
            .select_from(Position)
            .join(Instrument, Instrument.id == Position.instrument_id)
            .where(
                Position.quantity != 0,
                Instrument.venue.in_(enabled_venues),
            )
        )
        paper_runtime_risk_state = session.scalar(
            select(RuntimeRiskState.state).where(
                RuntimeRiskState.venue == "GATE_TESTNET_PAPER"
            )
        )
        gate_runtime_risk_state = session.scalar(
            select(RuntimeRiskState.state).where(RuntimeRiskState.venue == "GATE_TESTNET")
        )
        oanda_runtime_risk_state = session.scalar(
            select(RuntimeRiskState.state).where(
                RuntimeRiskState.venue == "OANDA_PRACTICE"
            )
        )
        paper_account_equity = session.scalar(
            select(PaperAccountState.equity).where(
                PaperAccountState.venue == "GATE_TESTNET_PAPER"
            )
        )
        healthy_market_streams = session.scalar(
            select(func.count())
            .select_from(MarketDataHealthState)
            .join(Instrument, Instrument.id == MarketDataHealthState.instrument_id)
            .where(
                MarketDataHealthState.status == "HEALTHY",
                Instrument.venue.in_(enabled_venues),
            )
        )
        unhealthy_market_streams = session.scalar(
            select(func.count())
            .select_from(MarketDataHealthState)
            .join(Instrument, Instrument.id == MarketDataHealthState.instrument_id)
            .where(
                MarketDataHealthState.status != "HEALTHY",
                Instrument.venue.in_(enabled_venues),
            )
        )
        strategy_signals = session.scalar(
            select(func.count())
            .select_from(StrategySignal)
            .join(Instrument, Instrument.id == StrategySignal.instrument_id)
            .where(Instrument.venue.in_(enabled_venues))
        )
        paper_trades = session.scalar(
            select(func.count()).select_from(Trade).where(Trade.venue == "GATE_TESTNET_PAPER")
        )
        closed_paper_positions = session.scalar(
            select(func.count())
            .select_from(PaperPositionControl)
            .join(Position, Position.id == PaperPositionControl.position_id)
            .join(Instrument, Instrument.id == Position.instrument_id)
            .where(
                PaperPositionControl.closed_at.is_not(None),
                Instrument.venue.in_(enabled_venues),
            )
        )
    return ExecutionSafetyStatus(
        approved_qualifications=int(approved or 0),
        rejected_qualifications=int(rejected or 0),
        active_orders=int(active_orders or 0),
        open_positions=int(open_positions or 0),
        runtime_risk_state=paper_runtime_risk_state or "NOT_INITIALIZED",
        healthy_market_streams=int(healthy_market_streams or 0),
        unhealthy_market_streams=int(unhealthy_market_streams or 0),
        strategy_signals=int(strategy_signals or 0),
        paper_trades=int(paper_trades or 0),
        closed_paper_positions=int(closed_paper_positions or 0),
        paper_account_equity=(
            str(paper_account_equity) if paper_account_equity is not None else None
        ),
        gate_runtime_risk_state=gate_runtime_risk_state or "MISSING",
        oanda_runtime_risk_state=oanda_runtime_risk_state or "MISSING",
    )
