"""按止损距离计算仓位的单元测试。"""

from decimal import Decimal

import pytest

from gold_crypto_quant.risk.position_sizing import calculate_position_size


def test_position_size_is_based_on_stop_risk() -> None:
    # 账户 10000、风险 0.25% 时最多亏 25；止损距离 0.5，所以数量应为 50。
    result = calculate_position_size(
        equity=Decimal("10000"), entry_price=Decimal("100"), stop_price=Decimal("99.5")
    )
    assert result.risk_amount == Decimal("25.00")
    assert result.quantity == Decimal("50.0")
    assert result.notional == Decimal("5000.0")
    assert result.required_margin == Decimal("40.0")


def test_position_size_rejects_zero_stop_distance() -> None:
    # 入场价和止损价相同会产生除零，必须在计算前明确拒绝。
    with pytest.raises(ValueError, match="must differ"):
        calculate_position_size(
            equity=Decimal("10000"), entry_price=Decimal("100"), stop_price=Decimal("100")
        )
