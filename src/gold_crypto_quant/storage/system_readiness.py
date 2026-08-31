"""从MySQL读取统一系统就绪检查所需的最新状态。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gold_crypto_quant.risk.system_readiness import (
    SystemReadinessEvaluation,
    evaluate_system_readiness,
)
from gold_crypto_quant.runtime.market_data_runner import (
    MARKET_DATA_SERVICE_NAME,
    OANDA_MARKET_DATA_SERVICE_NAME,
)
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import (
    Instrument,
    MarketDataHealthState,
    PaperSimulationState,
    RuntimeRiskState,
    ServiceRuntimeState,
    StrategyQualification,
)
from gold_crypto_quant.storage.paper_account import (
    OANDA_PAPER_ACCOUNT_VENUE,
    PAPER_ACCOUNT_VENUE,
)
from gold_crypto_quant.storage.execution_status import (
    ACTIVE_STRATEGY_NAME,
    ACTIVE_STRATEGY_VERSION,
)

GATE_EXPECTED_MARKET_STREAMS = 8
ALL_EXPECTED_MARKET_STREAMS = 12


@dataclass(frozen=True, slots=True)
class SystemReadinessSnapshot:
    """统一状态命令展示的数据库快照。"""

    evaluation: SystemReadinessEvaluation
    gate_service_status: str
    oanda_service_status: str
    gate_heartbeat_age_seconds: int | None
    oanda_heartbeat_age_seconds: int | None
    healthy_market_streams: int
    approved_qualifications: int
    gate_risk_state: str
    oanda_risk_state: str
    paper_simulation_status: str
    expected_market_streams: int
    oanda_enabled: bool


def _service_health(
    state: ServiceRuntimeState | None, now: datetime
) -> tuple[bool, str, int | None]:
    """按服务状态和其配置轮询周期判断心跳是否仍可用。"""
    if state is None:
        return False, "MISSING", None
    heartbeat = state.heartbeat_at.replace(tzinfo=UTC)
    age_seconds = max(0, int((now - heartbeat).total_seconds()))
    details = state.details or {}
    poll_seconds = float(details.get("poll_seconds", 60))
    allowed_age = max(180, int(poll_seconds * 3))
    healthy = state.status == "RUNNING" and age_seconds <= allowed_age
    return healthy, state.status, age_seconds


def read_system_readiness(
    *,
    live_trading: bool,
    oanda_enabled: bool = False,
    now: datetime | None = None,
    engine: Engine | None = None,
) -> SystemReadinessSnapshot:
    """按当前启用场所读取服务、风控、策略准入和30天监督状态。"""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("system readiness time must be timezone-aware")
    engine = engine or build_engine()
    with Session(engine) as session:
        services = {
            state.service_name: state
            for state in session.scalars(
                select(ServiceRuntimeState).where(
                    ServiceRuntimeState.service_name.in_(
                        (MARKET_DATA_SERVICE_NAME, OANDA_MARKET_DATA_SERVICE_NAME)
                    )
                )
            )
        }
        gate_healthy, gate_status, gate_age = _service_health(
            services.get(MARKET_DATA_SERVICE_NAME), now
        )
        oanda_healthy, oanda_status, oanda_age = _service_health(
            services.get(OANDA_MARKET_DATA_SERVICE_NAME), now
        )
        enabled_venues = (
            ("GATE_TESTNET", "OANDA_PRACTICE")
            if oanda_enabled
            else ("GATE_TESTNET",)
        )
        expected_market_streams = (
            ALL_EXPECTED_MARKET_STREAMS
            if oanda_enabled
            else GATE_EXPECTED_MARKET_STREAMS
        )
        # 调用品种关联过滤，只统计当前运行范围，历史OANDA健康记录不会污染Gate结论。
        healthy_streams = int(
            session.scalar(
                select(func.count())
                .select_from(MarketDataHealthState)
                .join(Instrument, Instrument.id == MarketDataHealthState.instrument_id)
                .where(
                    MarketDataHealthState.status == "HEALTHY",
                    Instrument.venue.in_(enabled_venues),
                )
            )
            or 0
        )
        risk_states = {
            venue: state
            for venue, state in session.execute(
                select(RuntimeRiskState.venue, RuntimeRiskState.state).where(
                    RuntimeRiskState.venue.in_(("GATE_TESTNET", "OANDA_PRACTICE"))
                )
            )
        }
        qualifications = list(
            session.scalars(
                select(StrategyQualification)
                .join(Instrument, Instrument.id == StrategyQualification.instrument_id)
                .where(
                    Instrument.venue.in_(enabled_venues),
                    StrategyQualification.strategy_name == ACTIVE_STRATEGY_NAME,
                    StrategyQualification.strategy_version == ACTIVE_STRATEGY_VERSION,
                )
                .order_by(
                    StrategyQualification.evaluated_at.desc(),
                    StrategyQualification.id.desc(),
                )
            )
        )
        seen_qualifications: set[tuple[int, str, str, str]] = set()
        approved = 0
        for qualification in qualifications:
            key = (
                qualification.instrument_id,
                qualification.interval_code,
                qualification.strategy_name,
                qualification.strategy_version,
            )
            if key in seen_qualifications:
                continue
            seen_qualifications.add(key)
            approved += qualification.decision == "APPROVED"
        simulation_venue = (
            OANDA_PAPER_ACCOUNT_VENUE if oanda_enabled else PAPER_ACCOUNT_VENUE
        )
        simulation = session.execute(
            select(PaperSimulationState).where(
                PaperSimulationState.venue == simulation_venue
            )
        ).scalar_one_or_none()

    gate_risk = risk_states.get("GATE_TESTNET", "MISSING")
    oanda_risk = risk_states.get("OANDA_PRACTICE", "MISSING")
    simulation_status = simulation.status if simulation is not None else "NOT_STARTED"
    # 调用纯就绪状态机，数据库层只负责提供最新事实。
    evaluation = evaluate_system_readiness(
        live_trading=live_trading,
        gate_service_healthy=gate_healthy,
        oanda_service_healthy=oanda_healthy,
        oanda_required=oanda_enabled,
        healthy_market_streams=healthy_streams,
        expected_market_streams=expected_market_streams,
        gate_risk_state=gate_risk,
        oanda_risk_state=oanda_risk,
        approved_qualifications=approved,
        paper_simulation_status=simulation_status,
    )
    return SystemReadinessSnapshot(
        evaluation=evaluation,
        gate_service_status=gate_status,
        oanda_service_status=oanda_status,
        gate_heartbeat_age_seconds=gate_age,
        oanda_heartbeat_age_seconds=oanda_age,
        healthy_market_streams=healthy_streams,
        approved_qualifications=approved,
        gate_risk_state=gate_risk,
        oanda_risk_state=oanda_risk,
        paper_simulation_status=simulation_status,
        expected_market_streams=expected_market_streams,
        oanda_enabled=oanda_enabled,
    )
