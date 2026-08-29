"""连接中断后的订单恢复决策。"""

from enum import StrEnum

from gold_crypto_quant.execution.states import TERMINAL_STATUSES, OrderStatus


class RecoveryAction(StrEnum):
    """恢复流程允许执行的安全动作。"""

    NO_ACTION = "NO_ACTION"
    QUERY_BY_CLIENT_ORDER_ID = "QUERY_BY_CLIENT_ORDER_ID"
    APPLY_REMOTE_STATUS = "APPLY_REMOTE_STATUS"
    MANUAL_REVIEW = "MANUAL_REVIEW"


def decide_recovery_action(
    local_status: OrderStatus,
    remote_status: OrderStatus | None,
) -> RecoveryAction:
    """根据本地与交易所状态决定恢复动作，永远不返回重新下单。"""
    if local_status in TERMINAL_STATUSES:
        if remote_status is None or remote_status == local_status:
            return RecoveryAction.NO_ACTION
        return RecoveryAction.MANUAL_REVIEW
    if remote_status is None:
        # SUBMITTING或UNKNOWN可能已经被交易所接受，必须按client_order_id查询而不能重发。
        return RecoveryAction.QUERY_BY_CLIENT_ORDER_ID
    if remote_status == local_status:
        return RecoveryAction.NO_ACTION
    return RecoveryAction.APPLY_REMOTE_STATUS
