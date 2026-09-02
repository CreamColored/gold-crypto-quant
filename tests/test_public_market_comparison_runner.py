"""双公共行情对照邮件摘要测试。"""

from gold_crypto_quant.runtime.bollinger_signal_cycle import BollingerSignalCycleSummary
from gold_crypto_quant.runtime.public_market_comparison_runner import (
    PUBLIC_COMPARISON_CONTRACTS,
    ComparisonFeedResult,
    PublicMarketComparisonRunner,
)


def test_public_comparison_contracts_include_gold() -> None:
    """双所对照必须同时覆盖BTC、ETH、XAU，避免后续误删黄金品种。"""
    assert PUBLIC_COMPARISON_CONTRACTS == ("BTC_USDT", "ETH_USDT", "XAU_USDT")


def test_comparison_email_summary_shows_both_equities_and_difference() -> None:
    """交易邮件必须同时展示两边影子权益，避免误读成同一账户。"""
    results = [
        ComparisonFeedResult(
            "Gate",
            10,
            BollingerSignalCycleSummary(
                status="SHADOW_RUNNING",
                new_signal_count=1,
                order_count=0,
                reason="test",
                paper_status="RUNNING",
                paper_equity=10_012.5,
            ),
        ),
        ComparisonFeedResult(
            "币安",
            10,
            BollingerSignalCycleSummary(
                status="SHADOW_RUNNING",
                new_signal_count=0,
                order_count=0,
                reason="test",
                paper_status="RUNNING",
                paper_equity=10_008.0,
            ),
        ),
    ]

    lines = PublicMarketComparisonRunner._comparison_status_lines(results)

    assert "Gate影子账户：权益 10012.50U / 状态 RUNNING / 新信号 1" in lines
    assert "币安影子账户：权益 10008.00U / 状态 RUNNING / 新信号 0" in lines
    assert "权益差（Gate-币安）：+4.50U" in lines
