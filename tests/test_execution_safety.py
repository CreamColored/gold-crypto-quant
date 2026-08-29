"""订单幂等、仓位限制和断线恢复状态机测试。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from gold_crypto_quant.execution.orders import OrderIntent, build_client_order_id
from gold_crypto_quant.execution.recovery import RecoveryAction, decide_recovery_action
from gold_crypto_quant.execution.risk_limits import PositionLimitPolicy, validate_position_limits
from gold_crypto_quant.execution.states import OrderStatus, validate_status_transition


def make_intent(**changes: object) -> OrderIntent:
    """创建默认的BTC测试开仓意图。"""
    values = {
        "strategy_run_id": "run-001",
        "qualification_hash": "a" * 64,
        "instrument_id": 1,
        "symbol": "BTC_USDT",
        "interval": "15m",
        "bar_time": datetime(2025, 1, 1, tzinfo=UTC),
        "side": "BUY",
        "position_side": "LONG",
        "quantity": Decimal("0.01"),
        "leverage": 10,
    }
    values.update(changes)
    return OrderIntent(**values)  # type: ignore[arg-type]


def test_client_order_id_is_deterministic() -> None:
    """同一订单意图无论重试多少次都必须生成相同编号。"""
    intent = make_intent()
    # 连续调用幂等编号方法，验证结果稳定且符合数据库64字符限制。
    first = build_client_order_id(intent)
    second = build_client_order_id(intent)
    assert first == second
    assert len(first) <= 64


def test_different_bar_produces_different_order_id() -> None:
    """下一根K线的相同方向信号不能复用上一根订单编号。"""
    first = make_intent()
    second = make_intent(bar_time=first.bar_time + timedelta(minutes=15))
    # 调用编号方法确认K线时间参与身份计算。
    assert build_client_order_id(first) != build_client_order_id(second)


def test_pyramiding_and_pending_entry_are_rejected() -> None:
    """已有仓位或待处理开仓单时都必须拒绝再次开仓。"""
    intent = make_intent()
    with pytest.raises(PermissionError, match="pyramiding"):
        # 调用仓位门禁，模拟BTC已经持仓。
        validate_position_limits(
            intent,
            open_instrument_ids={1},
            pending_entry_instrument_ids=set(),
        )
    with pytest.raises(PermissionError, match="already pending"):
        # 调用仓位门禁，模拟BTC提交状态未知或尚未成交。
        validate_position_limits(
            intent,
            open_instrument_ids=set(),
            pending_entry_instrument_ids={1},
        )


def test_position_count_and_leverage_limits_are_enforced() -> None:
    """全账户品种数和策略实际杠杆必须同时受限。"""
    intent = make_intent(instrument_id=3, leverage=20)
    with pytest.raises(PermissionError, match="maximum"):
        # 已有两个品种占用名额，策略限制为2时不能再开第三个品种。
        validate_position_limits(
            intent,
            open_instrument_ids={1, 2},
            pending_entry_instrument_ids=set(),
            policy=PositionLimitPolicy(maximum_open_instruments=2, leverage_limit=125),
        )
    with pytest.raises(PermissionError, match="leverage"):
        # 调用门禁并将策略杠杆上限设为10，拒绝20倍订单意图。
        validate_position_limits(
            intent,
            open_instrument_ids=set(),
            pending_entry_instrument_ids=set(),
            policy=PositionLimitPolicy(maximum_open_instruments=3, leverage_limit=10),
        )


def test_unknown_submission_is_queried_never_resent() -> None:
    """断线后无法确认是否提交成功时只能查询客户端订单号。"""
    # 调用恢复决策，remote_status=None代表交易所暂时无法返回订单。
    action = decide_recovery_action(OrderStatus.SUBMITTING, None)
    assert action == RecoveryAction.QUERY_BY_CLIENT_ORDER_ID


def test_terminal_status_cannot_be_revived() -> None:
    """已成交订单不能回退为已提交状态。"""
    with pytest.raises(ValueError, match="invalid order status transition"):
        # 调用状态转换校验，验证终态不可逆。
        validate_status_transition(OrderStatus.FILLED, OrderStatus.SUBMITTED)


def test_remote_terminal_conflict_requires_manual_review() -> None:
    """本地与远端终态冲突时禁止自动覆盖。"""
    # 调用恢复决策，本地已成交但远端显示取消属于严重状态冲突。
    action = decide_recovery_action(OrderStatus.FILLED, OrderStatus.CANCELED)
    assert action == RecoveryAction.MANUAL_REVIEW
