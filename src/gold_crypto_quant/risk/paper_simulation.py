"""连续30天模拟运行的纯监督规则，不包含任何真实交易开关。"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class PaperSimulationStatus(StrEnum):
    """模拟运行监督器可能产生的状态。"""

    RUNNING = "RUNNING"
    REVIEW_READY = "REVIEW_READY"
    RISK_FAILED = "RISK_FAILED"


@dataclass(frozen=True, slots=True)
class PaperSimulationDay:
    """一个UTC交易日的监督输入。"""

    trading_day: date
    is_complete: bool
    is_healthy: bool
    retry_count: int
    maximum_drawdown: Decimal
    risk_state: str


@dataclass(frozen=True, slots=True)
class PaperSimulationEvaluation:
    """30天监督规则的不可变计算结果。"""

    status: PaperSimulationStatus
    elapsed_days: int
    consecutive_healthy_days: int
    maximum_drawdown: Decimal
    service_incident_count: int
    reason: str


def evaluate_paper_simulation(
    days: list[PaperSimulationDay],
    *,
    required_days: int = 30,
    maximum_drawdown_limit: Decimal = Decimal("0.08"),
) -> PaperSimulationEvaluation:
    """评估完整UTC日；只有末尾连续30个健康日才进入人工复核状态。"""
    if required_days < 1:
        raise ValueError("required_days must be positive")
    if not Decimal("0") < maximum_drawdown_limit < Decimal("1"):
        raise ValueError("maximum_drawdown_limit must be between zero and one")

    completed_days = sorted(
        (day for day in days if day.is_complete), key=lambda day: day.trading_day
    )
    consecutive_healthy_days = 0
    for day in reversed(completed_days):
        if not day.is_healthy:
            break
        consecutive_healthy_days += 1

    maximum_drawdown = min(
        (day.maximum_drawdown for day in completed_days), default=Decimal("0")
    )
    service_incident_count = sum(
        day.retry_count + (0 if day.is_healthy else 1) for day in completed_days
    )
    drawdown_failed = any(
        day.risk_state == "DRAWDOWN_HALTED"
        or day.maximum_drawdown <= -maximum_drawdown_limit
        for day in completed_days
    )

    if drawdown_failed:
        status = PaperSimulationStatus.RISK_FAILED
        reason = "模拟账户达到8%最大回撤限制，稳定期失败并等待人工审核"
    elif consecutive_healthy_days >= required_days:
        status = PaperSimulationStatus.REVIEW_READY
        reason = f"已连续稳定运行{consecutive_healthy_days}个完整UTC日，可进入人工复核"
    else:
        status = PaperSimulationStatus.RUNNING
        reason = (
            f"连续健康运行{consecutive_healthy_days}/{required_days}个完整UTC日；"
            "未达到要求前继续模拟且真实交易保持关闭"
        )

    return PaperSimulationEvaluation(
        status=status,
        elapsed_days=len(completed_days),
        consecutive_healthy_days=consecutive_healthy_days,
        maximum_drawdown=maximum_drawdown,
        service_incident_count=service_incident_count,
        reason=reason,
    )
