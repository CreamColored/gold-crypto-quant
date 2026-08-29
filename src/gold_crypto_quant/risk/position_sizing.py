"""按照账户风险和止损距离计算仓位，杠杆只作为名义仓位上限。"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PositionSize:
    """一次仓位计算的结果，所有金额使用 Decimal 避免财务精度损失。"""

    quantity: Decimal
    notional: Decimal
    required_margin: Decimal
    risk_amount: Decimal


def calculate_position_size(
    *,
    equity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    risk_fraction: Decimal = Decimal("0.0025"),
    leverage: Decimal = Decimal("125"),
) -> PositionSize:
    """根据止损风险计算数量，并受最大杠杆约束。

    计算公式：允许亏损金额 = 账户权益 × 单笔风险比例；
    风险限制数量 = 允许亏损金额 ÷ 入场价与止损价之差。
    最终数量取风险限制和杠杆限制中的较小值。
    """
    # 先拒绝无效账户和价格，避免除零或产生负仓位。
    if equity <= 0 or entry_price <= 0 or stop_price <= 0:
        raise ValueError("equity and prices must be positive")
    if not Decimal("0") < risk_fraction <= Decimal("0.02"):
        raise ValueError("risk_fraction must be in (0, 0.02]")
    if not Decimal("1") <= leverage <= Decimal("125"):
        raise ValueError("leverage must be in [1, 125]")

    # 使用绝对距离，因此同一方法同时适用于多单和空单。
    stop_distance = abs(entry_price - stop_price)
    if stop_distance == 0:
        raise ValueError("stop price must differ from entry price")

    # 先按“最多允许亏多少钱”反推仓位，而不是根据 125 倍杠杆直接满仓。
    risk_amount = equity * risk_fraction
    risk_limited_quantity = risk_amount / stop_distance
    # 同时计算保证金允许的最大数量，防止名义价值超过账户权益乘以杠杆。
    leverage_limited_quantity = (equity * leverage) / entry_price
    quantity = min(risk_limited_quantity, leverage_limited_quantity)
    notional = quantity * entry_price

    # 返回完整结果，调用方可记录数量、名义价值、保证金占用和真实风险金额。
    return PositionSize(
        quantity=quantity,
        notional=notional,
        required_margin=notional / leverage,
        risk_amount=quantity * stop_distance,
    )
