"""钉钉自定义机器人推送；加签调用并在本地限流，避免触发平台限流丢失告警。"""

import base64
import hashlib
import hmac
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass

import httpx

DINGTALK_HOST = "oapi.dingtalk.com"
# 平台限制每个机器人每分钟20条，超过会被限流10分钟；留出余量并在本地先行拦截。
RATE_LIMIT_PER_MINUTE = 18
RATE_WINDOW_SECONDS = 60.0

SEVERITY_MARK = {
    "CRITICAL": "🔴",
    "WARNING": "🟠",
    "RECOVERED": "🟢",
    "INFO": "🔵",
}


class DingtalkError(RuntimeError):
    """钉钉接口返回错误码或网络调用失败。"""


@dataclass(frozen=True, slots=True)
class DingtalkMessage:
    """一条待发送的Markdown消息。"""

    title: str
    text: str


def build_markdown(
    *,
    event_title: str,
    event_lines: tuple[str, ...],
    severity: str,
    status_lines: tuple[str, ...] | None = None,
) -> DingtalkMessage:
    """把事件渲染成手机上可读的Markdown；标题带严重度标记便于扫一眼分辨。"""
    mark = SEVERITY_MARK.get(severity, "🔵")
    title = f"{mark} {event_title}"
    body = [f"### {title}", ""]
    body.extend(f"- {line}" for line in event_lines)
    if status_lines:
        body.append("")
        body.append("---")
        body.extend(f"- {line}" for line in status_lines)
    return DingtalkMessage(title=title, text="\n".join(body))


def _signed_url(webhook: str, secret: str, *, now: float | None = None) -> str:
    """按钉钉加签规则附加timestamp与sign；密钥只参与计算，不出现在URL可读部分。"""
    timestamp = str(round((now or time.time()) * 1000))
    payload = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode())
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


class DingtalkNotifier:
    """只负责推送，不落库；发送失败向上抛出由调用方决定是否影响主流程。"""

    def __init__(
        self,
        webhook: str | None,
        secret: str | None,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.enabled = bool(webhook and secret)
        self._webhook = webhook or ""
        self._secret = secret or ""
        if self.enabled and urllib.parse.urlsplit(self._webhook).hostname != DINGTALK_HOST:
            raise ValueError(f"Dingtalk webhook must point to {DINGTALK_HOST}")
        self._timeout = timeout
        self._transport = transport
        self._sent_at: deque[float] = deque()

    def _allow(self, now: float) -> bool:
        """滑动窗口限流；宁可本地丢弃一条，也不要让整个机器人被平台封禁10分钟。"""
        while self._sent_at and now - self._sent_at[0] >= RATE_WINDOW_SECONDS:
            self._sent_at.popleft()
        if len(self._sent_at) >= RATE_LIMIT_PER_MINUTE:
            return False
        self._sent_at.append(now)
        return True

    def send(self, message: DingtalkMessage, *, now: float | None = None) -> bool:
        """发送一条Markdown消息；被本地限流时返回False而不抛错。"""
        if not self.enabled:
            return False
        moment = now if now is not None else time.time()
        if not self._allow(moment):
            return False
        url = _signed_url(self._webhook, self._secret, now=moment)
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": message.title, "text": message.text},
        }
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(url, json=payload)
                data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise DingtalkError(f"Dingtalk request failed: {error}") from error
        if not isinstance(data, dict) or data.get("errcode") != 0:
            code = data.get("errcode") if isinstance(data, dict) else "UNKNOWN"
            text = data.get("errmsg") if isinstance(data, dict) else str(data)
            raise DingtalkError(f"Dingtalk API error {code}: {text}")
        return True
