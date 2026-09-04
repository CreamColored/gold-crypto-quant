"""双公共行情对照的邮件摘要与单轮耗时监控测试。"""

from datetime import UTC, datetime, timedelta

from gold_crypto_quant.runtime.bollinger_signal_cycle import BollingerSignalCycleSummary
from gold_crypto_quant.runtime.public_market_comparison_runner import (
    ALL_COMPARISON_CONTRACTS,
    BINANCE_LIVE_VENUE,
    GATE_LIVE_VENUE,
    PUBLIC_COMPARISON_CONTRACTS,
    PUBLIC_COMPARISON_VENUES,
    ComparisonFeedResult,
    CycleDurationWatch,
    FeedOutageWatch,
    PublicMarketComparisonRunner,
    _venue_state,
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


def test_all_contracts_still_include_gold() -> None:
    """可选品种表必须保留 XAU，避免代码里被误删。

    实际启用哪些由 .env 的 ACTIVE_SYMBOLS 决定——停跑某个品种是配置行为，
    不该靠删代码实现，否则想开回来就得改代码。
    """
    assert ALL_COMPARISON_CONTRACTS == ("BTC_USDT", "ETH_USDT", "XAU_USDT")
    assert set(PUBLIC_COMPARISON_CONTRACTS) <= set(ALL_COMPARISON_CONTRACTS)
    assert PUBLIC_COMPARISON_CONTRACTS, "不能把所有品种都停掉"


def test_disabled_venue_is_not_reported_as_a_fault() -> None:
    """配置里停掉的交易所显示"已停用"，不能报"本轮行情异常"。

    停跑是配置，不是故障。每分钟报一次假异常会让真正的故障淹没在噪声里。
    """
    for label, venue in (("Gate", GATE_LIVE_VENUE), ("币安", BINANCE_LIVE_VENUE)):
        expected = "本轮行情异常" if venue in PUBLIC_COMPARISON_VENUES else "已停用"
        assert _venue_state(label) == expected
    assert PUBLIC_COMPARISON_VENUES, "不能把所有交易所都停掉"


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

    expected = _venue_state("币安")
    assert f"币安影子账户：{expected}" in lines
    assert f"币安持仓：{expected}" in lines


def test_feed_pipeline_is_isolated_per_venue() -> None:
    """两个交易所的流水线不能共用任何可变入参，否则并行会互相污染。

    _run_feed 的每个参数都是该交易所专属的：拉取闭包、venue、状态文件路径。
    这个断言锁住"状态文件必须不同"——同一个路径会让两个线程互相覆盖影子账户。
    """
    from gold_crypto_quant.runtime.public_market_comparison_runner import (
        BINANCE_LIVE_STATE_PATH,
        GATE_LIVE_STATE_PATH,
    )

    assert GATE_LIVE_STATE_PATH != BINANCE_LIVE_STATE_PATH


def test_notifier_serialises_concurrent_sends(monkeypatch) -> None:
    """并行时两个交易所共用一个通知器，去重与限流都是读改写，必须串行化。"""
    from concurrent.futures import ThreadPoolExecutor

    from pydantic import SecretStr

    from gold_crypto_quant.config import Settings
    from gold_crypto_quant.notifications.runtime_events import RuntimeEventNotifier

    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.latest_paper_trade_id",
        lambda: 0,
    )
    settings = Settings(
        status_email_to="",
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=token",
        dingtalk_secret=SecretStr("SECdeadbeef"),
    )
    notifier = RuntimeEventNotifier(settings)
    pushed: list[str] = []
    monkeypatch.setattr(
        notifier, "_push_dingtalk", lambda **kwargs: pushed.append(kwargs["event_title"])
    )

    # 同一个不可重复事件被两个线程同时发送，只能有一个真正推送出去。
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result()
            for future in [
                pool.submit(
                    notifier.send,
                    event_key="runtime:SERVICE_STARTED",
                    event_title="服务启动",
                    event_lines=("详情：并发测试",),
                )
                for _ in range(2)
            ]
        ]

    assert len(pushed) == 1
    assert results == [False, False]


def test_short_feed_blip_never_alerts() -> None:
    """交易所与代理的短暂抖动不该打扰——2026-09-03下午币安那次503只持续3分钟。"""
    watch = FeedOutageWatch(alert_after=20)

    for _ in range(19):
        assert watch.on_failure() is False
    # 中途恢复一轮就清零，之后再失败19轮同样不告警。
    assert watch.on_success() is False
    for _ in range(19):
        assert watch.on_failure() is False
    assert watch.streak == 19


def test_alert_fires_once_at_the_threshold() -> None:
    """连续第20轮才告警，且一次故障只发一条——不能每轮都发。"""
    watch = FeedOutageWatch(alert_after=20)

    fired = [watch.on_failure() for _ in range(50)]

    assert fired.count(True) == 1
    assert fired.index(True) == 19


def test_flapping_produces_no_alert_pairs() -> None:
    """失败与成功交替出现时，不能连发好几对"中断+恢复"。

    这正是用户收到多条恢复通知的原因：告警原先按状态翻转触发，
    而抖动期间状态每轮都在翻。
    """
    watch = FeedOutageWatch(alert_after=20)
    alerts = recoveries = 0

    for _ in range(30):
        alerts += watch.on_failure()
        recoveries += watch.on_success()

    assert alerts == 0
    assert recoveries == 0


def test_recovery_notifies_once_after_a_real_outage() -> None:
    """真的告过警才报恢复，且只报一次。"""
    watch = FeedOutageWatch(alert_after=20)
    for _ in range(25):
        watch.on_failure()
    assert watch.alerted is True

    assert watch.on_success() is True
    assert watch.on_success() is False
    assert watch.streak == 0
