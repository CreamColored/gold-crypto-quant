"""SMTP运行状态报告测试；测试过程不会连接外部邮箱。"""

from gold_crypto_quant.notifications.smtp_mail import (
    StatusEmailMessage,
    _format_bytes,
    _format_uptime,
    send_smtp_email,
)


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
