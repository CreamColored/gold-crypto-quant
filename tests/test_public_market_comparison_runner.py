"""双公共行情对照的邮件摘要与单轮耗时监控测试。"""

from datetime import UTC, datetime, timedelta

from gold_crypto_quant.runtime.bollinger_signal_cycle import BollingerSignalCycleSummary
from gold_crypto_quant.runtime.public_market_comparison_runner import (
    PUBLIC_COMPARISON_CONTRACTS,
    ComparisonFeedResult,
    CycleDurationWatch,
    PublicMarketComparisonRunner,
)

START = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)


def test_cycle_within_poll_interval_stays_silent() -> None:
    """正常耗时不发任何通知，也不会在启动后第一轮就误报"已恢复"。"""
    watch = CycleDurationWatch(poll_seconds=20.0)

    assert watch.observe(9.0, START) == ""
    assert watch.observe(19.9, START + timedelta(seconds=20)) == ""
    assert watch.streak == 0


def test_first_overrun_alerts_immediately() -> None:
    """一超出轮询间隔就要告警——循环此时已经退化成只等0.1秒。"""
    watch = CycleDurationWatch(poll_seconds=20.0)

    assert watch.observe(119.0, START) == "overrun"
    assert watch.streak == 1


def test_sustained_overrun_is_throttled_by_cooldown() -> None:
    """持续超时按冷却期节流，不能每轮都发；冷却期满后再发一次。"""
    watch = CycleDurationWatch(poll_seconds=20.0, cooldown=timedelta(minutes=30))

    assert watch.observe(119.0, START) == "overrun"
    # 冷却期内无论超时多少轮都只记账、不告警。
    for minute in (1, 5, 15, 29):
        assert watch.observe(119.0, START + timedelta(minutes=minute)) == ""
    assert watch.streak == 5
    assert watch.observe(119.0, START + timedelta(minutes=30)) == "overrun"
    assert watch.streak == 6


def test_recovery_notifies_once_then_resets() -> None:
    """回落到间隔内发一次恢复通知，之后保持安静；再次超时重新立即告警。"""
    watch = CycleDurationWatch(poll_seconds=20.0)
    watch.observe(119.0, START)

    assert watch.observe(9.0, START + timedelta(minutes=1)) == "recovered"
    assert watch.streak == 0
    assert watch.observe(9.0, START + timedelta(minutes=2)) == ""
    # 恢复时清掉了冷却计时，所以下一次超时不受上一轮告警时间的压制。
    assert watch.observe(119.0, START + timedelta(minutes=3)) == "overrun"


def test_equal_to_poll_interval_is_not_an_overrun() -> None:
    """耗时正好等于间隔不算超时；此时循环仍会等满0.1秒，节奏没有丢。"""
    watch = CycleDurationWatch(poll_seconds=20.0)

    assert watch.observe(20.0, START) == ""


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
