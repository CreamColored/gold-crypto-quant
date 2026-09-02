"""SMTP运行状态报告测试；测试过程不会连接外部邮箱。"""

from datetime import UTC, datetime
from types import SimpleNamespace

from gold_crypto_quant.notifications.smtp_mail import (
    InlineEmailImage,
    StatusEmailMessage,
    _build_system_status_lines,
    _format_bytes,
    _format_uptime,
    build_gate_event_email,
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


def test_sends_html_and_inline_chart_through_ssl_smtp(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeSmtp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def login(self, username: str, password: str) -> None:
            return None

        def send_message(self, message) -> None:
            captured["message"] = message

    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.smtplib.SMTP_SSL",
        lambda *args, **kwargs: FakeSmtp(),
    )
    send_smtp_email(
        StatusEmailMessage(
            subject="图表测试",
            body="纯文本",
            html_body='<html><img src="cid:market-chart"></html>',
            inline_images=(InlineEmailImage(b"fake-png", "market-chart", "market-chart.png"),),
        ),
        host="smtp.example.com",
        port=465,
        username="sender@example.com",
        password="app-password",
        sender="sender@example.com",
        recipient="recipient@example.com",
    )

    message = captured["message"]
    content_types = [part.get_content_type() for part in message.walk()]
    assert "text/plain" in content_types
    assert "text/html" in content_types
    assert "image/png" in content_types


def test_system_status_uses_project_data_volume(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.psutil.cpu_percent",
        lambda **_kwargs: 12.3,
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.psutil.virtual_memory",
        lambda: SimpleNamespace(
            total=16 * 1024**3,
            available=8 * 1024**3,
            percent=50.0,
        ),
    )

    def fake_disk_usage(path: str):
        captured["disk_path"] = path
        return SimpleNamespace(
            total=200 * 1024**3,
            used=140 * 1024**3,
            free=60 * 1024**3,
            percent=70.0,
        )

    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.psutil.disk_usage",
        fake_disk_usage,
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.psutil.getloadavg",
        lambda: (1.0, 0.8, 0.5),
    )
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail.psutil.boot_time",
        lambda: datetime(2026, 8, 30, tzinfo=UTC).timestamp(),
    )

    lines = _build_system_status_lines(None, datetime(2026, 9, 1, tzinfo=UTC))

    assert captured["disk_path"] == str(tmp_path)
    assert any(
        "数据盘：总容量 200.00 GiB，已用 140.00 GiB，可用 60.00 GiB，使用率 70.0%" in line
        for line in lines
    )


def test_event_email_contains_trade_and_safety_context(monkeypatch) -> None:
    captured_chart: dict[str, object] = {}
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
    monkeypatch.setattr(
        "gold_crypto_quant.notifications.smtp_mail._build_market_chart_png",
        lambda **kwargs: captured_chart.update(kwargs) or b"fake-png",
    )

    # 调用事件模板，确认买卖信息和实盘硬关闭状态同时出现在邮件中。
    message = build_gate_event_email(
        datetime(2026, 8, 31, tzinfo=UTC),
        event_title="ETH_USDT 模拟买入开仓成交",
        event_lines=("成交价格：2500",),
        venue="BINANCE_LIVE_PUBLIC",
        comparison_status_lines=(
            "Gate影子账户：权益 10001.00U",
            "币安影子账户：权益 9999.00U",
            "真实交易：False",
            "交易所订单提交：False",
        ),
        process_id_override=123,
    )

    assert "模拟买入" in message.subject
    assert "成交价格：2500" in message.body
    assert "交易所订单提交：False" in message.body
    assert "Gate影子账户：权益 10001.00U" in message.body
    assert captured_chart["venue"] == "BINANCE_LIVE_PUBLIC"
    assert message.html_body is not None
    assert "cid:market-chart" in message.html_body
    assert len(message.inline_images) == 1
