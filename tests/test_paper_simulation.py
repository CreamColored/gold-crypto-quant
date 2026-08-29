"""连续30天模拟运行监督规则测试。"""

from datetime import date, timedelta
from decimal import Decimal

from gold_crypto_quant.risk.paper_simulation import (
    PaperSimulationDay,
    PaperSimulationStatus,
    evaluate_paper_simulation,
)


def healthy_days(count: int) -> list[PaperSimulationDay]:
    """构造连续健康的完整UTC日。"""
    start = date(2026, 1, 1)
    return [
        PaperSimulationDay(
            trading_day=start + timedelta(days=offset),
            is_complete=True,
            is_healthy=True,
            retry_count=0,
            maximum_drawdown=Decimal("-0.01"),
            risk_state="NORMAL",
        )
        for offset in range(count)
    ]


def test_requires_thirty_consecutive_healthy_days() -> None:
    evaluation = evaluate_paper_simulation(healthy_days(29))

    assert evaluation.status == PaperSimulationStatus.RUNNING
    assert evaluation.consecutive_healthy_days == 29


def test_thirty_healthy_days_only_becomes_review_ready() -> None:
    evaluation = evaluate_paper_simulation(healthy_days(30))

    assert evaluation.status == PaperSimulationStatus.REVIEW_READY
    assert "人工复核" in evaluation.reason


def test_unhealthy_day_resets_consecutive_count() -> None:
    days = healthy_days(31)
    days[-3] = PaperSimulationDay(
        trading_day=days[-3].trading_day,
        is_complete=True,
        is_healthy=False,
        retry_count=1,
        maximum_drawdown=Decimal("-0.02"),
        risk_state="NORMAL",
    )

    evaluation = evaluate_paper_simulation(days)

    assert evaluation.status == PaperSimulationStatus.RUNNING
    assert evaluation.consecutive_healthy_days == 2
    assert evaluation.service_incident_count == 2


def test_eight_percent_drawdown_fails_supervision() -> None:
    days = healthy_days(5)
    days[-1] = PaperSimulationDay(
        trading_day=days[-1].trading_day,
        is_complete=True,
        is_healthy=False,
        retry_count=0,
        maximum_drawdown=Decimal("-0.08"),
        risk_state="DRAWDOWN_HALTED",
    )

    evaluation = evaluate_paper_simulation(days)

    assert evaluation.status == PaperSimulationStatus.RISK_FAILED
