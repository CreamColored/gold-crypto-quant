"""Web监管后台用户、密码和审计存储。"""

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import AdminAuditLog, AppUser

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_MINUTES = 15


def hash_password(password: str) -> str:
    """使用带随机盐的scrypt保存密码，不依赖可逆加密或明文。"""
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(derived).decode(),
        )
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """重新派生摘要并使用常量时间比较，格式损坏时安全返回失败。"""
    try:
        algorithm, n, r, p, encoded_salt, encoded_digest = stored_hash.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt)
        expected = base64.urlsafe_b64decode(encoded_digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_admin_if_missing(engine: Engine | None = None) -> str | None:
    """首次运行创建纯监管admin并返回一次性初始密码；已存在时不重置。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        existing = session.scalar(select(AppUser).where(AppUser.username == "admin"))
        if existing is not None:
            return None
        initial_password = secrets.token_urlsafe(18)
        session.add(
            AppUser(
                username="admin",
                display_name="超级管理员",
                password_hash=hash_password(initial_password),
                role="SUPER_ADMIN",
                is_active=True,
                force_password_change=True,
                failed_login_count=0,
            )
        )
        session.commit()
        return initial_password


def authenticate_admin(
    username: str,
    password: str,
    *,
    engine: Engine | None = None,
) -> AppUser | None:
    """验证管理员并实施五次失败锁定十五分钟。"""
    engine = engine or build_engine()
    now = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as session:
        user = session.scalar(select(AppUser).where(AppUser.username == username.strip()))
        if user is None or not user.is_active or user.role != "SUPER_ADMIN":
            return None
        if user.locked_until is not None and user.locked_until > now:
            return None
        if not verify_password(password, user.password_hash):
            user.failed_login_count += 1
            if user.failed_login_count >= MAX_LOGIN_FAILURES:
                user.locked_until = now + timedelta(minutes=LOGIN_LOCK_MINUTES)
                user.failed_login_count = 0
            session.commit()
            return None
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def change_admin_password(
    user_id: int,
    current_password: str,
    new_password: str,
    *,
    engine: Engine | None = None,
) -> bool:
    """验证原密码后更新摘要，并解除首次登录强制修改标记。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        user = session.get(AppUser, user_id)
        if user is None or user.role != "SUPER_ADMIN":
            return False
        if not verify_password(current_password, user.password_hash):
            return False
        user.password_hash = hash_password(new_password)
        user.force_password_change = False
        session.commit()
        return True


def get_active_admin(user_id: int, engine: Engine | None = None) -> AppUser | None:
    """按会话用户ID读取仍处于启用状态的超级管理员。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        user = session.get(AppUser, user_id)
        if user is None or not user.is_active or user.role != "SUPER_ADMIN":
            return None
        session.expunge(user)
        return user


def save_admin_audit(
    *,
    user_id: int | None,
    action: str,
    result: str,
    ip_address: str | None,
    user_agent: str | None,
    details: dict[str, object] | None = None,
    engine: Engine | None = None,
) -> None:
    """保存不含密码、Cookie和API密钥的监管审计。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        session.add(
            AdminAuditLog(
                user_id=user_id,
                action=action,
                result=result,
                ip_address=ip_address,
                user_agent=(user_agent or "")[:512] or None,
                details=details,
            )
        )
        session.commit()
