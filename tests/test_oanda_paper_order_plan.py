"""OANDA黄金ATR止损、单位精度和保证金规则测试。"""

from decimal import Decimal

import pandas as pd
import pytest

from gold_crypto_quant.exchanges.oanda import OandaInstrumentRules
from gold_crypto_quant.risk.oanda_paper_order_plan import build_oanda_paper_order_plan


def _bars() -> pd.DataFrame:
    """构造真实波幅恒为2的连续黄金K线。"""
    index = pd.date_range("2026-01-01", periods=30, freq="30min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [2000.0] * 30,
            "high": [2001.0] * 30,
            "low": [1999.0] * 30,
            "close": [2000.0] * 30,
        },
        index=index,
    )


def _rules(**changes: object) -> OandaInstrumentRules:
    values = {
        "name": "XAU_USD",
        "display_precision": 3,
        "trade_units_precision": 0,
        "minimum_trade_size": Decimal("1"),
        "maximum_order_units": Decimal("10000"),
        "maximum_position_size": Decimal("0"),
        "margin_rate": Decimal("0.05"),
    }
    values.update(changes)
    return OandaInstrumentRules(**values)  # type: ignore[arg-type]


def test_oanda_plan_uses_margin_rate_and_integer_units() -> None:
    # ATR=2、2倍止损距离=4；25美元单笔风险向下取整为6单位，保证金率限制为20倍。
    plan = build_oanda_paper_order_plan(
        _bars(),
        side="LONG",
        entry_price=Decimal("2000"),
        equity=Decimal("10000"),
        available_margin=Decimal("1000"),
        rules=_rules(),
        atr_multiple=Decimal("2"),
    )

    assert plan.stop_price == Decimal("1996.000")
    assert plan.leverage == 20
    assert plan.contract_quantity == Decimal("6")
    assert plan.risk_amount == Decimal("24.000")
    assert plan.required_margin == Decimal("600")


def test_oanda_plan_rejects_margin_above_available_balance() -> None:
    with pytest.raises(PermissionError, match="available margin"):
        # 调用同一仓位计划，但模拟可用保证金不足时必须在创建订单前拒绝。
        build_oanda_paper_order_plan(
            _bars(),
            side="LONG",
            entry_price=Decimal("2000"),
            equity=Decimal("10000"),
            available_margin=Decimal("500"),
            rules=_rules(),
            atr_multiple=Decimal("2"),
        )
