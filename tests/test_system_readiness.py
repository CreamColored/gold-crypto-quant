"""全系统就绪阶段测试。"""

from gold_crypto_quant.risk.system_readiness import (
    SystemReadinessStatus,
    evaluate_system_readiness,
)


def evaluate(**changes: object):
    """构造基础设施全部健康的默认输入。"""
    values = {
        "live_trading": False,
        "gate_service_healthy": True,
        "oanda_service_healthy": True,
        "oanda_required": False,
        "healthy_market_streams": 8,
        "expected_market_streams": 8,
        "gate_risk_state": "NORMAL",
        "oanda_risk_state": "NORMAL",
        "approved_qualifications": 0,
        "paper_simulation_status": None,
    }
    values.update(changes)
    return evaluate_system_readiness(**values)  # type: ignore[arg-type]


def test_live_trading_true_is_always_unsafe() -> None:
    assert evaluate(live_trading=True).status == SystemReadinessStatus.UNSAFE


def test_stopped_service_is_not_ready() -> None:
    result = evaluate(gate_service_healthy=False)

    assert result.status == SystemReadinessStatus.NOT_READY
    assert "Gate行情服务" in result.blockers[0]


def test_paused_oanda_does_not_block_gate_scope() -> None:
    result = evaluate(
        oanda_service_healthy=False,
        oanda_risk_state="MISSING",
    )

    assert result.status == SystemReadinessStatus.OBSERVATION_READY


def test_enabled_oanda_still_fails_closed() -> None:
    result = evaluate(
        oanda_required=True,
        oanda_service_healthy=False,
        oanda_risk_state="MISSING",
        healthy_market_streams=8,
        expected_market_streams=12,
    )

    assert result.status == SystemReadinessStatus.NOT_READY
    assert any("OANDA行情服务" in blocker for blocker in result.blockers)


def test_healthy_infrastructure_without_approved_strategy_is_observation_only() -> None:
    result = evaluate()

    assert result.status == SystemReadinessStatus.OBSERVATION_READY


def test_approved_strategy_stays_paper_running_until_review_ready() -> None:
    assert (
        evaluate(approved_qualifications=1, paper_simulation_status="RUNNING").status
        == SystemReadinessStatus.PAPER_RUNNING
    )
    assert (
        evaluate(approved_qualifications=1, paper_simulation_status="REVIEW_READY").status
        == SystemReadinessStatus.REVIEW_READY
    )
