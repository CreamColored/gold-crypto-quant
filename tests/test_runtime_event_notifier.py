"""事件分流测试：成交只推钉钉，系统事件才进邮箱。"""

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from pydantic import SecretStr

from gold_crypto_quant.config import Settings
from gold_crypto_quant.notifications.dingtalk_bot import DingtalkError
from gold_crypto_quant.notifications.runtime_events import RuntimeEventNotifier
from gold_crypto_quant.runtime import public_market_comparison_runner as runner
from gold_crypto_quant.storage.trade_events import PaperTradeEvent


def test_each_new_trade_gets_its_own_email(monkeypatch) -> None:
    sent_subjects: list[str] = []
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.latest_paper_trade_id",
        lambda: 4,
    )
    events = (
        PaperTradeEvent(
            trade_id=5,
            symbol="ETH_USDT",
            order_side="BUY",
            position_side="LONG",
            quantity=Decimal("1"),
            price=Decimal("2500"),
            fee=Decimal("1.25"),
            realized_pnl=None,
            exit_reason=None,
            executed_at=datetime(2026, 8, 31, tzinfo=UTC),
        ),
        PaperTradeEvent(
            trade_id=6,
            symbol="ETH_USDT",
            order_side="SELL",
            position_side="LONG",
            quantity=Decimal("1"),
            price=Decimal("2508"),
            fee=Decimal("1.25"),
            realized_pnl=Decimal("8"),
            exit_reason="OPPOSITE_BAND",
            executed_at=datetime(2026, 8, 31, 1, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.read_paper_trade_events",
        lambda _after: events,
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.build_gate_event_email",
        lambda _now, **kwargs: SimpleNamespace(subject=kwargs["event_title"], body="body"),
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.send_smtp_email",
        lambda message, **_kwargs: sent_subjects.append(message.subject),
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.save_email_delivery",
        lambda **_kwargs: 1,
    )
    settings = Settings(
        status_email_to="recipient@example.com",
        smtp_host="smtp.example.com",
        smtp_username="sender@example.com",
        smtp_password=SecretStr("code"),
        smtp_from="sender@example.com",
    )
    notifier = RuntimeEventNotifier(settings)
    pushed: list[str] = []
    monkeypatch.setattr(
        notifier, "_push_dingtalk", lambda **kwargs: pushed.append(kwargs["event_title"])
    )

    # 调用成交扫描后，买入和卖出各推一条钉钉，且游标必须逐笔前进。
    count = notifier.notify_new_trades()

    assert count == 2
    assert notifier._last_trade_id == 6
    assert pushed == [
        "ETH_USDT 模拟买入开仓成交",
        "ETH_USDT 模拟卖出平仓成交",
    ]
    # 邮箱只留给系统事件；逐笔成交明细一封都不发。
    assert sent_subjects == []


def test_system_event_still_sends_email(monkeypatch) -> None:
    """服务起停、接口中断这类系统事件必须照常进邮箱。"""
    sent_subjects: list[str] = []
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.latest_paper_trade_id",
        lambda: 0,
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.build_gate_event_email",
        lambda _now, **kwargs: SimpleNamespace(subject=kwargs["event_title"], body="body"),
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.send_smtp_email",
        lambda message, **_kwargs: sent_subjects.append(message.subject),
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.save_email_delivery",
        lambda **_kwargs: 1,
    )
    settings = Settings(
        status_email_to="recipient@example.com",
        smtp_host="smtp.example.com",
        smtp_username="sender@example.com",
        smtp_password=SecretStr("code"),
        smtp_from="sender@example.com",
    )
    notifier = RuntimeEventNotifier(settings)
    monkeypatch.setattr(notifier, "_push_dingtalk", lambda **_kwargs: None)

    assert notifier.send(
        event_key="runtime:SERVICE_STOPPED",
        event_title="Gate行情服务停止",
        event_lines=("详情：手动停止",),
        severity="WARNING",
    )
    # 交易类事件即使配了邮箱也不发。
    assert not notifier.send(
        event_key="rotation-v5:开仓",
        event_title="模拟开仓：买入做多",
        event_lines=("品种：ETH_USDT",),
        category="TRADE",
    )

    assert sent_subjects == ["Gate行情服务停止"]


def test_dingtalk_failure_is_logged_not_swallowed(monkeypatch) -> None:
    """推送失败必须在日志里留痕：静默吞掉会让告警通道坏了都没人知道。"""
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.latest_paper_trade_id",
        lambda: 0,
    )
    logged: list[str] = []
    sent_mail: list[str] = []
    # 必须显式关掉邮件：Settings 会从 .env 读默认值，否则这个测试会真的发一封邮件。
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.send_smtp_email",
        lambda message, **_kwargs: sent_mail.append(message.subject),
    )
    settings = Settings(
        status_email_to="",
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=token",
        dingtalk_secret=SecretStr("SECdeadbeef"),
    )
    notifier = RuntimeEventNotifier(settings, reporter=logged.append)

    def explode(_message):
        raise DingtalkError("send text failed: 310000 keywords not in content")

    monkeypatch.setattr(notifier.dingtalk, "send", explode)

    # 推送失败不能让调用方抛异常，交易主流程必须继续。
    assert notifier.send(
        event_key="runtime:SERVICE_STARTED",
        event_title="服务启动",
        event_lines=("详情：测试",),
    ) is False
    assert len(logged) == 1
    assert "钉钉推送失败（服务启动）" in logged[0]
    assert "DingtalkError" in logged[0]
    # 日志里不能出现 webhook 的 access_token。
    assert "token" not in logged[0]
    assert sent_mail == []


def test_dingtalk_rate_limit_drop_is_logged(monkeypatch) -> None:
    """被本地限流丢弃也要留痕——爆发止损那几分钟最需要告警，静默丢掉最危险。"""
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.runtime_events.latest_paper_trade_id",
        lambda: 0,
    )
    logged: list[str] = []
    settings = Settings(
        status_email_to="",
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=token",
        dingtalk_secret=SecretStr("SECdeadbeef"),
    )
    notifier = RuntimeEventNotifier(settings, reporter=logged.append)
    # send 返回 False 表示被限流丢弃，不抛异常。
    monkeypatch.setattr(notifier.dingtalk, "send", lambda _message: False)

    notifier.send(
        event_key="rotation-v5:止损",
        event_title="模拟平仓：固定保护止损",
        event_lines=("品种：BTC_USDT",),
        category="TRADE",
    )

    assert len(logged) == 1
    assert "钉钉推送被本地限流丢弃" in logged[0]
"""服务启停不推送的行为锁定。

重启是日常操作——今天一天就重启了六七次。每次都推邮件和钉钉，真正的异常（行情
中断、单轮超时、Redis降级）会淹在这些噪声里。日志仍然留痕。
"""

def test_runner_does_not_notify_on_start_or_stop() -> None:
    source = inspect.getsource(runner.PublicMarketComparisonRunner.run)
    assert '"started"' not in source
    assert '"stopped"' not in source
    # 日志必须还在，否则重启就完全无痕
    assert "量化服务启动" in source
    assert "量化服务停止" in source


def test_anomaly_alerts_are_kept() -> None:
    """行情中断、单轮超时、Redis降级这些异常告警不受影响。"""
    source = inspect.getsource(runner)
    for key in ("source-degraded", "source-recovered", "cycle-overrun", "cycle-duration-recovered"):
        assert f'"{key}"' in source
    assert "feed" in source
