"""按交易和异常事件即时发送SMTP邮件，并在单次服务运行中去重。"""

import os
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from gold_crypto_quant.config import Settings
from gold_crypto_quant.notifications.dingtalk_bot import (
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


class RuntimeEventNotifier:
    """将每笔模拟成交和重要状态变化转换为即时邮件。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
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
        """推送到钉钉；任何失败都吞掉，告警通道不得反过来影响交易主流程。"""
        if not self.dingtalk.enabled:
            return
        try:
            self.dingtalk.send(
                build_markdown(
                    event_title=event_title,
                    event_lines=event_lines,
                    severity=severity,
                    status_lines=status_lines,
                )
            )
        except (DingtalkError, OSError, ValueError):
            pass

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
    ) -> bool:
        """发送并审计一封事件邮件；相同非重复事件在本进程中只发送一次。

        钉钉与邮件是两条独立通道：邮件未配置时钉钉照常推送，反之亦然。
        """
        if not repeatable and event_key in self._sent_keys:
            return False
        now = now or datetime.now(UTC)
        self._push_dingtalk(
            event_title=event_title,
            event_lines=event_lines,
            severity=severity,
            status_lines=comparison_status_lines,
        )
        if not self.enabled:
            # 邮件未启用时也要记账，否则同一事件每轮都会重复推送钉钉。
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
        """逐笔发送游标之后的新成交；失败的邮件留到下一轮重试。"""
        if not self.enabled:
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
            success = self.send(
                event_key=f"trade:{event.trade_id}",
                event_title=f"{event.symbol} 模拟{action}{lifecycle}",
                event_lines=lines,
                severity=(
                    "CRITICAL"
                    if event.exit_reason in {"ATR_STOP", "STOP", "RISK_EXIT"}
                    else "INFO"
                ),
                now=datetime.now(UTC),
            )
            if not success:
                break
            self._last_trade_id = event.trade_id
            sent += 1
        return sent
