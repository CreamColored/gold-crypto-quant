"""订单发送前的仓位和待处理订单限制。"""

from dataclasses import dataclass

from gold_crypto_quant.execution.orders import OrderIntent


@dataclass(frozen=True, slots=True)
class PositionLimitPolicy:
    """禁止补仓并限制全账户同时持仓品种数。"""

    maximum_open_instruments: int = 3
    leverage_limit: int = 125

    def __post_init__(self) -> None:
        if self.maximum_open_instruments < 1:
            raise ValueError("maximum_open_instruments must be positive")
        if not 1 <= self.leverage_limit <= 125:
            raise ValueError("leverage_limit must be in [1, 125]")


def validate_position_limits(
    intent: OrderIntent,
    *,
    open_instrument_ids: set[int],
    pending_entry_instrument_ids: set[int],
    policy: PositionLimitPolicy | None = None,
) -> None:
    """验证禁止补仓、重复待处理开仓和总持仓数量限制。"""
    policy = policy or PositionLimitPolicy()
    if intent.leverage > policy.leverage_limit:
        raise PermissionError("order leverage exceeds position policy")
    if intent.reduce_only:
        if intent.instrument_id not in open_instrument_ids:
            raise PermissionError("reduce-only order requires an existing position")
        return
    if intent.instrument_id in open_instrument_ids:
        raise PermissionError("pyramiding is forbidden for an open instrument")
    if intent.instrument_id in pending_entry_instrument_ids:
        raise PermissionError("an entry order is already pending for this instrument")
    combined_instruments = open_instrument_ids | pending_entry_instrument_ids
    if len(combined_instruments) >= policy.maximum_open_instruments:
        raise PermissionError("maximum number of open instruments reached")
