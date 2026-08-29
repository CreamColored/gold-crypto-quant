"""SMTP状态邮件发送结果的MySQL审计写入。"""

from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import EmailDeliveryLog


def save_email_delivery(
    *,
    recipient: str,
    subject: str,
    status: str,
    attempted_at: datetime,
    error_message: str | None = None,
    engine: Engine | None = None,
) -> int:
    """保存一次发送结果；正文、用户名和SMTP授权码永不进入数据库。"""
    if attempted_at.tzinfo is None:
        raise ValueError("email delivery time must be timezone-aware")
    if status not in {"SENT", "FAILED"}:
        raise ValueError("email delivery status must be SENT or FAILED")
    database_time = attempted_at.astimezone(UTC).replace(tzinfo=None)
    record = EmailDeliveryLog(
        channel="SMTP",
        recipient=recipient,
        subject=subject,
        status=status,
        error_message=error_message,
        attempted_at=database_time,
        sent_at=database_time if status == "SENT" else None,
    )
    engine = engine or build_engine()
    with Session(engine) as session, session.begin():
        # 调用ORM保存单次审计，主键可用于后续邮件状态查询和故障定位。
        session.add(record)
        session.flush()
        return record.id
