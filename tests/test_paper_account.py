"""模拟账户余额、权益和可用保证金计算测试。"""

from decimal import Decimal

from gold_crypto_quant.risk.paper_account import calculate_paper_account


def test_paper_equity_includes_fees_realized_and_unrealized_pnl() -> None:
    # 初始10000，加100已实现、减10手续费和50浮亏，最终权益应为10040。
    result = calculate_paper_account(
        initial_equity=Decimal("10000"),
        realized_pnl=Decimal("100"),
        unrealized_pnl=Decimal("-50"),
        total_fees=Decimal("10"),
        used_margin=Decimal("500"),
    )

    assert result.balance == Decimal("10090")
    assert result.equity == Decimal("10040")
    assert result.available_margin == Decimal("9540")


def test_available_margin_never_becomes_negative() -> None:
    result = calculate_paper_account(
        initial_equity=Decimal("100"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("-90"),
        total_fees=Decimal("0"),
        used_margin=Decimal("20"),
    )

    assert result.equity == Decimal("10")
    assert result.available_margin == Decimal("0")
