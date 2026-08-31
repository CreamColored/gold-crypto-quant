"""即时成交邮件的游标和逐笔发送测试。"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from pydantic import SecretStr

from gold_crypto_quant.config import Settings
from gold_crypto_quant.notifications.runtime_events import RuntimeEventNotifier
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

    # 调用成交扫描后，买入和卖出必须分别生成邮件，不能合并成周期摘要。
    count = notifier.notify_new_trades()

    assert count == 2
    assert sent_subjects == [
        "ETH_USDT 模拟买入开仓成交",
        "ETH_USDT 模拟卖出平仓成交",
    ]
