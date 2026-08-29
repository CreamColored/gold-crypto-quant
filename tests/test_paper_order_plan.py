"""ATR止损、Gate合约张数取整和保证金限制测试。"""

from decimal import Decimal

import pandas as pd
import pytest

from gold_crypto_quant.exchanges.gate import GateContract
from gold_crypto_quant.risk.paper_order_plan import build_paper_order_plan


def _contract(**changes: object) -> GateContract:
    """构造整数张的Gate USDT永续合约规则。"""
    values = {
        "name": "BTC_USDT",
        "leverage_min": Decimal("1"),
        "leverage_max": Decimal("100"),
        "order_size_min": Decimal("1"),
        "order_size_max": Decimal("100000"),
        "quanto_multiplier": Decimal("0.001"),
        "enable_decimal": False,
        "order_price_round": Decimal("0.1"),
        "mark_price": Decimal("100"),
        "index_price": Decimal("100"),
        "maker_fee_rate": Decimal("-0.00005"),
        "taker_fee_rate": Decimal("0.0005"),
        "in_delisting": False,
    }
    values.update(changes)
    return GateContract(**values)  # type: ignore[arg-type]


def _bars() -> pd.DataFrame:
    """构造真实波幅恒为2的连续K线。"""
    index = pd.date_range("2026-01-01", periods=30, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.0] * 30,
        },
        index=index,
    )


def test_atr_risk_is_converted_to_integer_gate_contracts() -> None:
    # ATR=2、1.5倍止损得到97；账户风险25 USDT决定基础数量并向下取整到合约张数。
    plan = build_paper_order_plan(
        _bars(),
        side="LONG",
        entry_price=Decimal("100"),
        equity=Decimal("10000"),
        available_margin=Decimal("1000"),
        contract=_contract(),
    )

    assert plan.stop_price == Decimal("97.0")
    assert plan.leverage == 100
    assert plan.contract_quantity == Decimal("8333")
    assert plan.base_quantity == Decimal("8.333")
    assert plan.risk_amount == Decimal("24.9990")
    assert plan.required_margin == Decimal("8.333")


def test_order_plan_rejects_insufficient_available_margin() -> None:
    with pytest.raises(PermissionError, match="available margin"):
        # 订单风险虽然合规，但可用保证金低于8.333时仍必须拒绝。
        build_paper_order_plan(
            _bars(),
            side="LONG",
            entry_price=Decimal("100"),
            equity=Decimal("10000"),
            available_margin=Decimal("1"),
            contract=_contract(),
        )


def test_decimal_contract_lots_fail_closed() -> None:
    with pytest.raises(PermissionError, match="decimal"):
        # 未获得明确小数张步长前，enable_decimal品种不能猜测取整规则。
        build_paper_order_plan(
            _bars(),
            side="SHORT",
            entry_price=Decimal("100"),
            equity=Decimal("10000"),
            available_margin=Decimal("1000"),
            contract=_contract(enable_decimal=True),
        )
