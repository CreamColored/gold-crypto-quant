"""订单状态机及允许的单向转换。"""

from enum import StrEnum


class OrderStatus(StrEnum):
    """本地订单生命周期状态。"""

    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED})

ALLOWED_TRANSITIONS = {
    OrderStatus.CREATED: {OrderStatus.SUBMITTING, OrderStatus.CANCELED},
    OrderStatus.SUBMITTING: {
        OrderStatus.SUBMITTED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.SUBMITTED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.UNKNOWN: {
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    },
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELED: set(),
    OrderStatus.REJECTED: set(),
}


def validate_status_transition(current: OrderStatus, target: OrderStatus) -> None:
    """拒绝回退、终态复活和其他未定义的订单状态变化。"""
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid order status transition: {current} -> {target}")
