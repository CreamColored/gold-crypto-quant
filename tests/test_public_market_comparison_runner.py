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


def test_isolated_slow_cycles_do_not_alert() -> None:
    """偶发一两轮变慢不告警——网络抖动和交易所响应慢不值得打扰。"""
    watch = CycleDurationWatch(poll_seconds=20.0)

    assert watch.observe(119.0, START) == ""
    assert watch.observe(119.0, START + timedelta(seconds=120)) == ""
    assert watch.streak == 2
    # 中间恢复一轮就清零，之后再慢两轮同样不告警。
    assert watch.observe(9.0, START + timedelta(seconds=140)) == ""
    assert watch.streak == 0
    assert watch.observe(119.0, START + timedelta(seconds=160)) == ""


def test_third_consecutive_overrun_alerts() -> None:
    """连续第三轮超时才告警，此时轮询节奏确实已经丢了。"""
    watch = CycleDurationWatch(poll_seconds=20.0)

    assert watch.observe(119.0, START) == ""
    assert watch.observe(119.0, START + timedelta(seconds=120)) == ""
    assert watch.observe(119.0, START + timedelta(seconds=240)) == "overrun"
    assert watch.streak == 3


def test_sustained_overrun_is_throttled_by_cooldown() -> None:
    """持续超时按15分钟冷却期节流，不能每轮都发；冷却期满后再发一次。"""
    watch = CycleDurationWatch(poll_seconds=20.0, cooldown=timedelta(minutes=15))
    for offset in (0, 2, 4):
        watch.observe(119.0, START + timedelta(minutes=offset))
    assert watch.last_alert == START + timedelta(minutes=4)

    for minute in (5, 10, 18):
        assert watch.observe(119.0, START + timedelta(minutes=minute)) == ""
    assert watch.observe(119.0, START + timedelta(minutes=19)) == "overrun"


def test_recovery_only_notifies_when_an_alert_was_sent() -> None:
    """没告过警就不该报恢复；告过警的才发一次恢复通知并重置。"""
    quiet = CycleDurationWatch(poll_seconds=20.0)
    quiet.observe(119.0, START)
    quiet.observe(119.0, START + timedelta(minutes=1))
    # 只慢了两轮、从未告警，恢复时保持安静。
    assert quiet.observe(9.0, START + timedelta(minutes=2)) == ""

    alerted = CycleDurationWatch(poll_seconds=20.0)
    for offset in (0, 1, 2):
        alerted.observe(119.0, START + timedelta(minutes=offset))
    assert alerted.observe(9.0, START + timedelta(minutes=3)) == "recovered"
    assert alerted.streak == 0
    assert alerted.observe(9.0, START + timedelta(minutes=4)) == ""


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
                paper_holdings="BTC 多 0.079385 @77497.70 现价77600.00（浮动+8.13U）",
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
                paper_holdings="全部空仓",
            ),
        ),
    ]

    lines = PublicMarketComparisonRunner._comparison_status_lines(results)

    assert "Gate影子账户：权益 10012.50U / 状态 RUNNING / 新信号 1" in lines
    assert "币安影子账户：权益 10008.00U / 状态 RUNNING / 新信号 0" in lines
    assert "权益差（Gate-币安）：+4.50U" in lines
    # 每条交易通知都要能直接看出此刻手里还有什么，不用再去翻后台。
    assert "Gate持仓：BTC 多 0.079385 @77497.70 现价77600.00（浮动+8.13U）" in lines
    assert "币安持仓：全部空仓" in lines


def test_status_lines_mark_missing_feed_for_both_equity_and_holdings() -> None:
    """某个交易所本轮失败时，权益和持仓都要显式标注异常，不能静默显示成空仓。"""
    results = [
        ComparisonFeedResult(
            "Gate",
            10,
            BollingerSignalCycleSummary(
                status="SHADOW_RUNNING",
                new_signal_count=0,
                order_count=0,
                reason="test",
                paper_status="RUNNING",
                paper_equity=10_000.0,
                paper_holdings="全部空仓",
            ),
        )
    ]

    lines = PublicMarketComparisonRunner._comparison_status_lines(results)

    assert "币安影子账户：本轮行情异常" in lines
    assert "币安持仓：本轮行情异常" in lines
