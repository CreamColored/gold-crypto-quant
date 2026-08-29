"""根据ATR、账户风险和Gate合约规则构造本地模拟订单计划。"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

import pandas as pd

from gold_crypto_quant.exchanges.gate import GateContract
from gold_crypto_quant.risk.indicators import average_true_range
from gold_crypto_quant.risk.position_sizing import calculate_position_size


@dataclass(frozen=True, slots=True)
class PaperOrderPlan:
    """完成风险限制和合约换算后的不可变开仓计划。"""

    entry_price: Decimal
    stop_price: Decimal
    atr: Decimal
    atr_multiple: Decimal
    base_quantity: Decimal
    contract_quantity: Decimal
    notional: Decimal
    required_margin: Decimal
    risk_amount: Decimal
    leverage: int


def _round_stop_price(price: Decimal, tick_size: Decimal, side: str) -> Decimal:
    """按价格最小单位向风险更保守的方向取整止损价。"""
    if tick_size <= 0:
        raise ValueError("order price tick must be positive")
    rounding = ROUND_DOWN if side == "LONG" else ROUND_UP
    return (price / tick_size).to_integral_value(rounding=rounding) * tick_size


def build_paper_order_plan(
    bars: pd.DataFrame,
    *,
    side: str,
    entry_price: Decimal,
    equity: Decimal,
    available_margin: Decimal,
    contract: GateContract,
    risk_fraction: Decimal = Decimal("0.0025"),
    requested_leverage: int = 125,
    atr_period: int = 14,
    atr_multiple: Decimal = Decimal("1.5"),
) -> PaperOrderPlan:
    """计算ATR止损并把基础资产数量转换为Gate合约张数。"""
    if side not in {"LONG", "SHORT"}:
        raise ValueError("paper order side must be LONG or SHORT")
    if available_margin <= 0:
        raise PermissionError("available margin must be positive")
    if contract.in_delisting:
        raise PermissionError("Gate contract is delisting")
    if contract.enable_decimal:
        # Gate没有独立返回小数张步长；BTC/ETH当前为整数张，其他类型先失败关闭。
        raise PermissionError("decimal Gate contract sizing is not supported yet")
    if contract.quanto_multiplier <= 0:
        raise ValueError("Gate quanto multiplier must be positive")
    if atr_multiple <= 0:
        raise ValueError("ATR multiple must be positive")

    # 调用Wilder ATR并只读取最新已收盘K线；预热不足或脏数据时不允许构造订单。
    atr_value = average_true_range(bars, atr_period).iloc[-1]
    if pd.isna(atr_value) or float(atr_value) <= 0:
        raise PermissionError("latest ATR is unavailable or non-positive")
    atr = Decimal(str(float(atr_value)))
    raw_stop = (
        entry_price - atr * atr_multiple
        if side == "LONG"
        else entry_price + atr * atr_multiple
    )
    # 调用价格步长取整，随后用最终止损距离重新计算仓位风险。
    stop_price = _round_stop_price(raw_stop, contract.order_price_round, side)
    if stop_price <= 0 or stop_price == entry_price:
        raise PermissionError("rounded ATR stop price is invalid")

    exchange_leverage_max = int(contract.leverage_max.to_integral_value(rounding=ROUND_DOWN))
    leverage = min(requested_leverage, exchange_leverage_max)
    if leverage < int(contract.leverage_min.to_integral_value(rounding=ROUND_UP)):
        raise PermissionError("requested leverage is below Gate contract minimum")
    # 调用统一风险仓位方法，先得到标的币数量上限，再换算成合约张数。
    position_size = calculate_position_size(
        equity=equity,
        entry_price=entry_price,
        stop_price=stop_price,
        risk_fraction=risk_fraction,
        leverage=Decimal(leverage),
    )
    raw_contracts = position_size.quantity / contract.quanto_multiplier
    contract_quantity = raw_contracts.to_integral_value(rounding=ROUND_DOWN)
    contract_quantity = min(contract_quantity, contract.order_size_max)
    if contract_quantity < contract.order_size_min:
        raise PermissionError("risk-sized quantity is below Gate minimum order size")

    base_quantity = contract_quantity * contract.quanto_multiplier
    notional = base_quantity * entry_price
    required_margin = notional / Decimal(leverage)
    risk_amount = base_quantity * abs(entry_price - stop_price)
    if required_margin > available_margin:
        raise PermissionError("paper order required margin exceeds available margin")
    if risk_amount > equity * risk_fraction:
        raise RuntimeError("rounded paper order exceeds configured risk amount")
    return PaperOrderPlan(
        entry_price=entry_price,
        stop_price=stop_price,
        atr=atr,
        atr_multiple=atr_multiple,
        base_quantity=base_quantity,
        contract_quantity=contract_quantity,
        notional=notional,
        required_margin=required_margin,
        risk_amount=risk_amount,
        leverage=leverage,
    )
