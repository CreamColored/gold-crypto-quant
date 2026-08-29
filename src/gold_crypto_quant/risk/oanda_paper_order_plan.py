"""根据ATR、账户风险和OANDA品种规则构造黄金模拟订单计划。"""

from decimal import ROUND_DOWN, Decimal

import pandas as pd

from gold_crypto_quant.exchanges.oanda import OandaInstrumentRules
from gold_crypto_quant.risk.indicators import average_true_range
from gold_crypto_quant.risk.paper_order_plan import PaperOrderPlan, _round_stop_price
from gold_crypto_quant.risk.position_sizing import calculate_position_size


def build_oanda_paper_order_plan(
    bars: pd.DataFrame,
    *,
    side: str,
    entry_price: Decimal,
    equity: Decimal,
    available_margin: Decimal,
    rules: OandaInstrumentRules,
    risk_fraction: Decimal = Decimal("0.0025"),
    requested_leverage: int = 125,
    atr_period: int = 14,
    atr_multiple: Decimal = Decimal("1.5"),
) -> PaperOrderPlan:
    """计算ATR止损，并按OANDA单位精度和保证金率限制仓位。"""
    if side not in {"LONG", "SHORT"}:
        raise ValueError("OANDA paper order side must be LONG or SHORT")
    if entry_price <= 0 or available_margin <= 0:
        raise PermissionError("OANDA price and available margin must be positive")
    if rules.name != "XAU_USD":
        raise PermissionError("only XAU_USD is enabled for OANDA paper execution")
    if rules.display_precision < 0 or rules.trade_units_precision < 0:
        raise ValueError("OANDA precision must be non-negative")
    if not Decimal("0") < rules.margin_rate <= Decimal("1"):
        raise ValueError("OANDA margin rate must be in (0, 1]")
    if atr_multiple <= 0:
        raise ValueError("ATR multiple must be positive")

    # 调用Wilder ATR并只读取最新已收盘K线，预热不足时失败关闭。
    atr_value = average_true_range(bars, atr_period).iloc[-1]
    if pd.isna(atr_value) or float(atr_value) <= 0:
        raise PermissionError("latest ATR is unavailable or non-positive")
    atr = Decimal(str(float(atr_value)))
    raw_stop = (
        entry_price - atr * atr_multiple
        if side == "LONG"
        else entry_price + atr * atr_multiple
    )
    price_tick = Decimal(1).scaleb(-rules.display_precision)
    # 调用统一止损取整方法，多单向下、空单向上，避免取整缩短止损距离。
    stop_price = _round_stop_price(raw_stop, price_tick, side)
    if stop_price <= 0 or stop_price == entry_price:
        raise PermissionError("rounded OANDA ATR stop price is invalid")

    # OANDA保证金率决定账户真实最高杠杆；125倍只是系统上限，不能覆盖品种规则。
    margin_leverage = int((Decimal("1") / rules.margin_rate).to_integral_value(
        rounding=ROUND_DOWN
    ))
    leverage = min(requested_leverage, margin_leverage)
    if leverage < 1:
        raise PermissionError("OANDA margin rules do not permit a valid leverage")
    # 调用统一风险仓位方法，按止损亏损和实际杠杆的较小限制计算黄金单位数。
    position_size = calculate_position_size(
        equity=equity,
        entry_price=entry_price,
        stop_price=stop_price,
        risk_fraction=risk_fraction,
        leverage=Decimal(leverage),
    )
    unit_step = Decimal(1).scaleb(-rules.trade_units_precision)
    units = (position_size.quantity / unit_step).to_integral_value(
        rounding=ROUND_DOWN
    ) * unit_step
    units = min(units, rules.maximum_order_units)
    if rules.maximum_position_size > 0:
        units = min(units, rules.maximum_position_size)
    if units < rules.minimum_trade_size:
        raise PermissionError("risk-sized quantity is below OANDA minimum trade size")

    notional = units * entry_price
    required_margin = notional / Decimal(leverage)
    risk_amount = units * abs(entry_price - stop_price)
    if required_margin > available_margin:
        raise PermissionError("OANDA paper order required margin exceeds available margin")
    if risk_amount > equity * risk_fraction:
        raise RuntimeError("rounded OANDA paper order exceeds configured risk amount")
    return PaperOrderPlan(
        entry_price=entry_price,
        stop_price=stop_price,
        atr=atr,
        atr_multiple=atr_multiple,
        base_quantity=units,
        contract_quantity=units,
        notional=notional,
        required_margin=required_margin,
        risk_amount=risk_amount,
        leverage=leverage,
    )
