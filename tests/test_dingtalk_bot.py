"""钉钉机器人加签、限流与错误处理测试。"""

import json

import httpx
import pytest

from gold_crypto_quant.notifications.dingtalk_bot import (
    RATE_LIMIT_PER_MINUTE,
    DingtalkError,
    DingtalkNotifier,
    _signed_url,
    build_markdown,
)

WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=token"


def _ok_handler(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    return handler


def test_signed_url_carries_timestamp_and_sign() -> None:
    """加签必须把timestamp与sign附在查询串上，密钥本身不得出现在URL里。"""
    url = _signed_url(WEBHOOK, "SECRET", now=1_700_000_000.0)

    assert "timestamp=1700000000000" in url
    assert "sign=" in url
    assert "SECRET" not in url


def test_rejects_non_dingtalk_host() -> None:
    """只允许官方域名，避免配置被换到未知服务器。"""
    with pytest.raises(ValueError, match="must point to"):
        DingtalkNotifier("https://example.com/robot/send", "SECRET")


def test_disabled_without_credentials() -> None:
    """缺少任一凭据即视为未启用，静默跳过而不是报错。"""
    assert DingtalkNotifier(None, None).enabled is False
    assert DingtalkNotifier(WEBHOOK, None).enabled is False
    assert DingtalkNotifier(None, "SECRET").enabled is False


def test_sends_markdown_payload() -> None:
    seen: list[httpx.Request] = []
    notifier = DingtalkNotifier(
        WEBHOOK, "SECRET", transport=httpx.MockTransport(_ok_handler(seen))
    )

    assert notifier.send(build_markdown(
        event_title="止损",
        event_lines=("品种：ETH_USDT",),
        severity="CRITICAL",
    )) is True
    payload = json.loads(seen[0].content)
    assert payload["msgtype"] == "markdown"
    assert "🔴" in payload["markdown"]["title"]
    assert "品种：ETH_USDT" in payload["markdown"]["text"]


def test_local_rate_limit_stops_before_platform_ban() -> None:
    """本地窗口先行拦截，宁可丢一条也不要让机器人被平台限流十分钟。"""
    seen: list[httpx.Request] = []
    notifier = DingtalkNotifier(
        WEBHOOK, "SECRET", transport=httpx.MockTransport(_ok_handler(seen))
    )
    message = build_markdown(event_title="t", event_lines=(), severity="INFO")

    accepted = sum(notifier.send(message, now=1000.0) for _ in range(RATE_LIMIT_PER_MINUTE + 5))

    assert accepted == RATE_LIMIT_PER_MINUTE
    # 窗口滑过之后重新放行。
    assert notifier.send(message, now=1000.0 + 61) is True


def test_api_error_code_raises() -> None:
    """接口返回非零错误码要抛出，交由上层决定是否降级。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"})

    notifier = DingtalkNotifier(WEBHOOK, "SECRET", transport=httpx.MockTransport(handler))

    with pytest.raises(DingtalkError, match="310000"):
        notifier.send(build_markdown(event_title="t", event_lines=(), severity="INFO"))
