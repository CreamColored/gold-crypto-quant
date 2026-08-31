"""SMTP运行状态报告测试；测试过程不会连接外部邮箱。"""

from gold_crypto_quant.notifications.smtp_mail import (
    StatusEmailMessage,
    build_gate_event_email,
    _format_bytes,
    _format_uptime,
    send_smtp_email,
)
from datetime import UTC, datetime
from types import SimpleNamespace


def test_formats_mac_resource_values_for_email() -> None:
    assert _format_bytes(3 * 1024**3) == "3.00 GiB"
    assert _format_uptime((2 * 24 + 3) * 3600 + 4 * 60) == "2天 3小时 4分钟"


def test_sends_message_through_ssl_smtp(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeSmtp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def login(self, username: str, password: str) -> None:
            captured["login"] = (username, password)

        def send_message(self, message) -> None:
            captured["message"] = message

    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.smtplib.SMTP_SSL",
        lambda *args, **kwargs: FakeSmtp(),
    )

    send_smtp_email(
        StatusEmailMessage(subject="测试主题", body="测试正文"),
        host="smtp.example.com",
        port=465,
        username="sender@example.com",
        password="app-password",
        sender="sender@example.com",
        recipient="recipient@example.com",
    )

    assert captured["login"] == ("sender@example.com", "app-password")
    message = captured["message"]
    assert message["To"] == "recipient@example.com"
    assert message["Subject"] == "测试主题"


def test_event_email_contains_trade_and_safety_context(monkeypatch) -> None:
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.read_service_state",
        lambda _name: SimpleNamespace(process_id=None),
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.read_execution_safety_status",
        lambda **_kwargs: SimpleNamespace(
            approved_qualifications=0,
            active_orders=0,
            open_positions=0,
            paper_account_equity="993.08",
            gate_runtime_risk_state="NORMAL",
            runtime_risk_state="NORMAL",
        ),
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail._build_system_status_lines",
        lambda *_args: ("Mac服务器状态：测试",),
    )

    # 调用事件模板，确认买卖信息和实盘硬关闭状态同时出现在邮件中。
    message = build_gate_event_email(
        datetime(2026, 8, 31, tzinfo=UTC),
        event_title="ETH_USDT 模拟买入开仓成交",
        event_lines=("成交价格：2500",),
    )

    assert "模拟买入" in message.subject
    assert "成交价格：2500" in message.body
    assert "交易所订单提交：False" in message.body
