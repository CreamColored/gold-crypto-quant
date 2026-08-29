"""本地模拟成交价格、止损触发和盈亏的纯计算规则。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal

import pandas as pd


@dataclass(frozen=True, slots=True)
class StopTrigger:
    """第一根触发保护止损的已收盘K线及无滑点参考价。"""

    bar_time: datetime
    reference_price: Decimal


def calculate_adverse_fill_price(
    reference_price: Decimal,
    *,
    order_side: str,
    price_tick: Decimal,
    slippage_rate: Decimal,
) -> Decimal:
    """按买高卖低施加滑点，并按价格步长向不利方向取整。"""
    if reference_price <= 0 or price_tick <= 0:
        raise ValueError("fill reference price and tick must be positive")
    if not Decimal("0") <= slippage_rate <= Decimal("0.05"):
        raise ValueError("paper fill slippage must be in [0, 0.05]")
    if order_side == "BUY":
        slipped = reference_price * (Decimal("1") + slippage_rate)
        rounding = ROUND_UP
    elif order_side == "SELL":
        slipped = reference_price * (Decimal("1") - slippage_rate)
        rounding = ROUND_DOWN
    else:
        raise ValueError("paper fill side must be BUY or SELL")
    return (slipped / price_tick).to_integral_value(rounding=rounding) * price_tick


def find_first_stop_trigger(
    bars: pd.DataFrame,
    *,
    position_side: str,
    stop_price: Decimal,
    after_bar_time: datetime | None = None,
) -> StopTrigger | None:
    """按时间查找首根触发止损的已收盘K线，并处理跳空越过止损。"""
    if position_side not in {"LONG", "SHORT"}:
        raise ValueError("position side must be LONG or SHORT")
    if stop_price <= 0:
        raise ValueError("stop price must be positive")
    if bars.empty:
        return None
    if bars.index.tz is None:
        raise ValueError("stop evaluation bars must be timezone-aware")
    candidates = bars
    if after_bar_time is not None:
        if after_bar_time.tzinfo is None:
            raise ValueError("after_bar_time must be timezone-aware")
        candidates = bars.loc[bars.index > pd.Timestamp(after_bar_time)]
    for bar_time, bar in candidates.iterrows():
        open_price = Decimal(str(bar["open"]))
        if position_side == "LONG" and Decimal(str(bar["low"])) <= stop_price:
            # 多头向下跳空时只能按更低开盘价成交，否则按止损价触发。
            reference = min(open_price, stop_price)
            return StopTrigger(bar_time=bar_time.to_pydatetime(), reference_price=reference)
        if position_side == "SHORT" and Decimal(str(bar["high"])) >= stop_price:
            # 空头向上跳空时只能按更高开盘价成交，否则按止损价触发。
            reference = max(open_price, stop_price)
            return StopTrigger(bar_time=bar_time.to_pydatetime(), reference_price=reference)
    return None


def calculate_position_pnl(
    *,
    position_side: str,
    entry_price: Decimal,
    exit_price: Decimal,
    contract_quantity: Decimal,
    contract_multiplier: Decimal,
) -> Decimal:
    """按Gate USDT合约张数和乘数计算未扣费盈亏。"""
    base_quantity = contract_quantity * contract_multiplier
    if position_side == "LONG":
        return (exit_price - entry_price) * base_quantity
    if position_side == "SHORT":
        return (entry_price - exit_price) * base_quantity
    raise ValueError("position side must be LONG or SHORT")
