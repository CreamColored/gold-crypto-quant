"""按事件类别分发通知：邮箱只收系统事件，行情交易明细走钉钉。"""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy.exc import SQLAlchemyError

from gold_crypto_quant.config import Settings
from gold_crypto_quant.notifications.dingtalk_bot import (
    RATE_LIMIT_PER_MINUTE,
    DingtalkError,
    DingtalkNotifier,
    build_markdown,
)
from gold_crypto_quant.notifications.smtp_mail import (
    build_gate_event_email,
    send_smtp_email,
)
from gold_crypto_quant.storage.email_delivery import save_email_delivery
from gold_crypto_quant.storage.trade_events import (
    latest_paper_trade_id,
    read_paper_trade_events,
)

# 邮箱只承载系统事件：服务起停、行情接口中断与恢复、风控熔断、策略阻断状态。
# 开仓、减仓、平仓这类逐笔行情交易明细量大且不需要留邮件底稿，只推钉钉。
EMAIL_SUPPRESSED_CATEGORIES = frozenset({"TRADE"})


class RuntimeEventNotifier:
    """把运行期事件分发到邮件和钉钉两条独立通道。"""

    def __init__(
        self,
        settings: Settings,
        *,
        reporter: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        # 推送失败不能影响交易主流程，但必须在日志里留痕；静默吞掉会让通道坏了都没人知道。
        self.reporter = reporter or (lambda _message: None)
        # 量化服务并行跑两个交易所时共用本对象：_sent_keys 去重、钉钉滑动窗口限流
        # 和邮件游标都是读改写，必须串行化，否则限流可能被同时放行而触发平台封禁。
        self._lock = Lock()
        self.enabled = bool(settings.status_email_to)
        self._sent_keys: set[str] = set()
        # 服务重启时从当前最大成交开始，只通知本次运行后产生的新成交。
        self._last_trade_id = latest_paper_trade_id() if self.enabled else 0
        # 钉钉是独立通道：未配置时静默跳过，配置了也不能让它的故障影响邮件与策略。
        self.dingtalk = DingtalkNotifier(
            settings.dingtalk_webhook,
            (
                settings.dingtalk_secret.get_secret_value()
                if settings.dingtalk_secret is not None
                else None
            ),
        )

    def _push_dingtalk(
        self,
        *,
        event_title: str,
        event_lines: tuple[str, ...],
        severity: str,
        status_lines: tuple[str, ...] | None,
    ) -> None:
        """推送到钉钉；失败只记日志不抛出，告警通道不得反过来影响交易主流程。"""
        if not self.dingtalk.enabled:
            return
        try:
            delivered = self.dingtalk.send(
                build_markdown(
                    event_title=event_title,
                    event_lines=event_lines,
                    severity=severity,
                    status_lines=status_lines,
                )
            )
            if not delivered:
                # 本地限流丢弃同样要留痕：集中爆发止损那几分钟最需要告警，
                # 静默丢掉会让人误以为通道正常。
                self.reporter(
                    f"钉钉推送被本地限流丢弃（{event_title}）："
                    f"每分钟上限{RATE_LIMIT_PER_MINUTE}条"
                )
        except (DingtalkError, OSError, ValueError) as error:
            # 只输出异常类型与消息，webhook 的 access_token 不进日志。
            self.reporter(f"钉钉推送失败（{event_title}）：{type(error).__name__}: {error}")

    def send(
        self,
        *,
        event_key: str,
        event_title: str,
        event_lines: tuple[str, ...],
        severity: str = "INFO",
        now: datetime | None = None,
        repeatable: bool = False,
        venue: str | None = None,
        comparison_status_lines: tuple[str, ...] | None = None,
        category: str = "SYSTEM",
    ) -> bool:
        """分发一条事件；相同非重复事件在本进程中只处理一次。

        钉钉与邮件是两条独立通道：邮件未配置时钉钉照常推送，反之亦然。
        category为TRADE的行情交易明细只推钉钉，不进邮箱；返回值表示邮件是否发出。
        """
        with self._lock:
            return self._send_locked(
                event_key=event_key,
                event_title=event_title,
                event_lines=event_lines,
                severity=severity,
                now=now,
                repeatable=repeatable,
                venue=venue,
                comparison_status_lines=comparison_status_lines,
                category=category,
            )

    def _send_locked(
        self,
        *,
        event_key: str,
        event_title: str,
        event_lines: tuple[str, ...],
        severity: str,
        now: datetime | None,
        repeatable: bool,
        venue: str | None,
        comparison_status_lines: tuple[str, ...] | None,
        category: str,
    ) -> bool:
        """send() 的实际实现；调用方必须已持有 _lock。"""
        if not repeatable and event_key in self._sent_keys:
            return False
        now = now or datetime.now(UTC)
        self._push_dingtalk(
            event_title=event_title,
            event_lines=event_lines,
            severity=severity,
            status_lines=comparison_status_lines,
        )
        if not self.enabled or category in EMAIL_SUPPRESSED_CATEGORIES:
            # 不走邮件时也要记账，否则同一事件每轮都会重复推送钉钉。
            if not repeatable:
                self._sent_keys.add(event_key)
            return False
        message = build_gate_event_email(
            now,
            event_title=event_title,
            event_lines=event_lines,
            severity=severity,
            **({"venue": venue} if venue is not None else {}),
            comparison_status_lines=comparison_status_lines,
            # 事件由哪个服务发出，就展示哪个进程的资源占用，避免沿用旧服务PID。
            process_id_override=os.getpid() if comparison_status_lines is not None else None,
        )
        status = "SENT"
        error_message: str | None = None
        try:
            # 调用QQ SSL SMTP即时发送；授权码只从SecretStr取出并停留在内存中。
            send_smtp_email(
                message,
                host=self.settings.smtp_host or "",
                port=self.settings.smtp_port,
                username=self.settings.smtp_username or "",
                password=(
                    self.settings.smtp_password.get_secret_value()
                    if self.settings.smtp_password is not None
                    else ""
                ),
                sender=self.settings.smtp_from or "",
                recipient=self.settings.status_email_to or "",
            )
        except RuntimeError as error:
            status = "FAILED"
            error_message = str(error)
        finally:
            try:
                # 调用邮件审计只保存标题和结果，正文及SMTP授权码永不写入数据库。
                save_email_delivery(
                    recipient=self.settings.status_email_to or "",
                    subject=message.subject,
                    status=status,
                    attempted_at=now,
                    error_message=error_message,
                )
            except (OSError, RuntimeError, ValueError, SQLAlchemyError):
                pass
        if status == "SENT":
            self._sent_keys.add(event_key)
            return True
        return False

    def notify_new_trades(self) -> int:
        """逐笔推送游标之后的新成交；成交明细只走钉钉，不再进邮箱。"""
        if not (self.enabled or self.dingtalk.enabled):
            return 0
        sent = 0
        for event in read_paper_trade_events(self._last_trade_id):
            action = "买入" if event.order_side == "BUY" else "卖出"
            lifecycle = "开仓成交" if event.realized_pnl is None else "平仓成交"
            lines = (
                f"成交ID：{event.trade_id}",
                f"品种：{event.symbol}",
                f"订单动作：{action}",
                f"持仓方向：{event.position_side}",
                f"成交数量：{event.quantity}",
                f"成交价格：{event.price}",
                f"手续费：{event.fee} USDT",
                f"已实现毛盈亏：{event.realized_pnl if event.realized_pnl is not None else '-'}",
                f"退出原因：{event.exit_reason or '-'}",
                f"成交时间UTC：{event.executed_at:%Y-%m-%d %H:%M:%S}",
            )
            self.send(
                event_key=f"trade:{event.trade_id}",
                event_title=f"{event.symbol} 模拟{action}{lifecycle}",
                event_lines=lines,
                severity=(
                    "CRITICAL"
                    if event.exit_reason in {"ATR_STOP", "STOP", "RISK_EXIT"}
                    else "INFO"
                ),
                now=datetime.now(UTC),
                category="TRADE",
            )
            # 成交不再走邮件，钉钉的失败已在推送层吞掉，没有可重试的对象，游标照常前进。
            self._last_trade_id = event.trade_id
            sent += 1
        return sent
