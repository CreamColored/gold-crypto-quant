"""全系统观察、模拟运行和人工复核就绪状态机。"""

from dataclasses import dataclass
from enum import StrEnum


class SystemReadinessStatus(StrEnum):
    """系统从未就绪到30天复核就绪的阶段。"""

    UNSAFE = "UNSAFE"
    NOT_READY = "NOT_READY"
    OBSERVATION_READY = "OBSERVATION_READY"
    PAPER_RUNNING = "PAPER_RUNNING"
    REVIEW_READY = "REVIEW_READY"


@dataclass(frozen=True, slots=True)
class SystemReadinessEvaluation:
    """统一就绪检查的不可变输出。"""

    status: SystemReadinessStatus
    blockers: tuple[str, ...]


def evaluate_system_readiness(
    *,
    live_trading: bool,
    gate_service_healthy: bool,
    oanda_service_healthy: bool,
    oanda_required: bool,
    healthy_market_streams: int,
    expected_market_streams: int,
    gate_risk_state: str,
    oanda_risk_state: str,
    approved_qualifications: int,
    paper_simulation_status: str | None,
) -> SystemReadinessEvaluation:
    """按基础设施、风控、准入和30天监督顺序评估系统阶段。"""
    safety_blockers: list[str] = []
    if live_trading:
        safety_blockers.append("LIVE_TRADING必须保持false")
    required_risk_states = [("Gate", gate_risk_state)]
    if oanda_required:
        required_risk_states.append(("OANDA", oanda_risk_state))
    for venue, risk_state in required_risk_states:
        if risk_state in {"DAILY_HALTED", "DRAWDOWN_HALTED"}:
            safety_blockers.append(f"{venue}账户风控状态为{risk_state}")
    if safety_blockers:
        return SystemReadinessEvaluation(
            status=SystemReadinessStatus.UNSAFE,
            blockers=tuple(safety_blockers),
        )

    infrastructure_blockers: list[str] = []
    if not gate_service_healthy:
        infrastructure_blockers.append("Gate行情服务未持续运行或心跳已过期")
    if oanda_required and not oanda_service_healthy:
        infrastructure_blockers.append("OANDA行情服务未持续运行或心跳已过期")
    if healthy_market_streams != expected_market_streams:
        infrastructure_blockers.append(
            f"健康行情流{healthy_market_streams}/{expected_market_streams}"
        )
    if gate_risk_state != "NORMAL":
        infrastructure_blockers.append(f"Gate账户风控缺失或异常：{gate_risk_state}")
    if oanda_required and oanda_risk_state != "NORMAL":
        infrastructure_blockers.append(f"OANDA账户风控缺失或异常：{oanda_risk_state}")
    if infrastructure_blockers:
        if approved_qualifications < 1:
            infrastructure_blockers.append("没有通过样本外准入的策略，禁止启动模拟仓位")
        return SystemReadinessEvaluation(
            status=SystemReadinessStatus.NOT_READY,
            blockers=tuple(infrastructure_blockers),
        )

    if approved_qualifications < 1:
        return SystemReadinessEvaluation(
            status=SystemReadinessStatus.OBSERVATION_READY,
            blockers=("没有通过样本外准入的策略，禁止启动模拟仓位",),
        )
    if paper_simulation_status not in {"RUNNING", "REVIEW_READY"}:
        return SystemReadinessEvaluation(
            status=SystemReadinessStatus.OBSERVATION_READY,
            blockers=("获准策略尚未初始化模拟资金账本",),
        )
    if paper_simulation_status == "REVIEW_READY":
        return SystemReadinessEvaluation(
            status=SystemReadinessStatus.REVIEW_READY,
            blockers=(),
        )
    return SystemReadinessEvaluation(
        status=SystemReadinessStatus.PAPER_RUNNING,
        blockers=("30天连续稳定模拟尚未达到人工复核要求",),
    )
