"""本地模拟成交滑点、保护止损和合约盈亏测试。"""

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd

from gold_crypto_quant.execution.paper_fills import (
    calculate_adverse_fill_price,
    calculate_position_pnl,
    find_first_stop_trigger,
)


def test_adverse_slippage_rounds_against_the_order() -> None:
    # 买单滑点后向上取价格步长，卖单滑点后向下取价格步长。
    buy = calculate_adverse_fill_price(
        Decimal("100"),
        order_side="BUY",
        price_tick=Decimal("0.1"),
        slippage_rate=Decimal("0.0002"),
    )
    sell = calculate_adverse_fill_price(
        Decimal("100"),
        order_side="SELL",
        price_tick=Decimal("0.1"),
        slippage_rate=Decimal("0.0002"),
    )

    assert buy == Decimal("100.1")
    assert sell == Decimal("99.9")


def test_long_stop_uses_worse_open_when_price_gaps_below_stop() -> None:
    index = pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 94.0],
            "high": [101.0, 96.0],
            "low": [99.0, 93.0],
            "close": [100.0, 95.0],
        },
        index=index,
    )

    # 第一根未触发95止损，第二根跳空至94时不能乐观地按95成交。
    trigger = find_first_stop_trigger(
        bars,
        position_side="LONG",
        stop_price=Decimal("95"),
        after_bar_time=datetime(2025, 12, 31, 23, 55, tzinfo=UTC),
    )

    assert trigger is not None
    assert trigger.bar_time == index[1].to_pydatetime()
    assert trigger.reference_price == Decimal("94.0")


def test_contract_pnl_uses_contract_multiplier_for_both_directions() -> None:
    values = {
        "entry_price": Decimal("100"),
        "exit_price": Decimal("105"),
        "contract_quantity": Decimal("20"),
        "contract_multiplier": Decimal("0.01"),
    }

    # 20张、每张0.01标的等于0.2标的，价格变化5对应1 USDT盈亏。
    assert calculate_position_pnl(position_side="LONG", **values) == Decimal("1.00")
    assert calculate_position_pnl(position_side="SHORT", **values) == Decimal("-1.00")
