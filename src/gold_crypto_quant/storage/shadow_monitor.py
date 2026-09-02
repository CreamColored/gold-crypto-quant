"""把Gate和币安影子账户状态持久化为Web监管数据。"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.runtime.bollinger_signal_cycle import BollingerSignalCycleSummary
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import (
    ShadowEquitySnapshot,
    ShadowTradeEvent,
    TradingAccount,
)

ACCOUNT_DEFINITIONS = {
    "GATE_LIVE_PUBLIC": {
        "account_code": "SYSTEM_GATE_LIVE_SHADOW",
        "display_name": "Gate影子账户",
    },
    "BINANCE_LIVE_PUBLIC": {
        "account_code": "SYSTEM_BINANCE_LIVE_SHADOW",
        "display_name": "币安影子账户",
    },
}


def _ensure_account(session: Session, venue: str) -> TradingAccount:
    """幂等登记系统实验账户，明确禁止交易。"""
    definition = ACCOUNT_DEFINITIONS[venue]
    statement = mysql_insert(TradingAccount).values(
        account_code=definition["account_code"],
        display_name=definition["display_name"],
        owner_user_id=None,
        owner_type="SYSTEM",
        venue=venue,
        market_type="CRYPTO_FUTURES",
        environment="SHADOW",
        currency="USDT",
        strategy_version="BOLLINGER_ROTATION_V5_7",
        is_active=True,
        trading_enabled=False,
    )
    session.execute(
        statement.on_duplicate_key_update(
            display_name=statement.inserted.display_name,
            strategy_version=statement.inserted.strategy_version,
            is_active=True,
            trading_enabled=False,
        )
    )
    session.flush()
    return session.scalar(
        select(TradingAccount).where(
            TradingAccount.account_code == definition["account_code"]
        )
    )


def ensure_system_shadow_accounts(engine: Engine | None = None) -> None:
    """在Web或双行情服务启动时保证两套系统账户存在。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        for venue in ACCOUNT_DEFINITIONS:
            _ensure_account(session, venue)
        session.commit()


def _event_type(title: str) -> str:
    if "开仓" in title:
        return "OPEN"
    if "减仓" in title:
        return "REDUCE"
    if "平仓" in title:
        return "CLOSE"
    if "熔断" in title or "止损" in title:
        return "RISK"
    return "STATUS"


def _event_details(lines: tuple[str, ...]) -> dict[str, str]:
    """把邮件友好的中文行转换为Web可检索的键值对象。"""
    details: dict[str, str] = {}
    for line in lines:
        if "：" in line:
            key, value = line.split("：", 1)
            details[key] = value
        else:
            details[f"说明{len(details) + 1}"] = line
    return details


def record_shadow_cycle(
    venue: str,
    state_path: Path,
    summary: BollingerSignalCycleSummary,
    *,
    engine: Engine | None = None,
    now: datetime | None = None,
) -> None:
    """保存一分钟权益快照和本轮新交易事件，不改变策略状态文件。"""
    if venue not in ACCOUNT_DEFINITIONS:
        raise ValueError(f"unsupported shadow venue: {venue}")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("shadow snapshot time must be timezone-aware")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    positions = state.get("positions", {})
    open_position_count = sum(
        1 for position in positions.values() if position.get("position_side")
    )
    risk_state = (
        "DRAWDOWN_HALTED"
        if state.get("permanent_fuse")
        else "DAILY_HALTED" if state.get("daily_blocked") else "NORMAL"
    )
    database_time = now.astimezone(UTC).replace(tzinfo=None, second=0, microsecond=0)
    engine = engine or build_engine()
    with Session(engine) as session:
        account = _ensure_account(session, venue)
        snapshot = mysql_insert(ShadowEquitySnapshot).values(
            trading_account_id=account.id,
            snapshot_time=database_time,
            equity=Decimal(str(state["equity"])),
            peak_equity=Decimal(str(state["peak_equity"])),
            day_start_equity=Decimal(str(state["day_start_equity"])),
            open_position_count=open_position_count,
            risk_state=risk_state,
        )
        session.execute(
            snapshot.on_duplicate_key_update(
                equity=snapshot.inserted.equity,
                peak_equity=snapshot.inserted.peak_equity,
                day_start_equity=snapshot.inserted.day_start_equity,
                open_position_count=snapshot.inserted.open_position_count,
                risk_state=snapshot.inserted.risk_state,
            )
        )
        for event in summary.paper_events:
            details = _event_details(event.lines)
            event_statement = mysql_insert(ShadowTradeEvent).values(
                event_key=f"{venue}:{event.event_key}",
                trading_account_id=account.id,
                event_time=database_time,
                symbol=details.get("品种", "-"),
                interval_code=details.get("交易周期", "-"),
                title=event.title,
                event_type=_event_type(event.title),
                severity=event.severity,
                details=details,
            )
            session.execute(event_statement.on_duplicate_key_update(title=event_statement.inserted.title))
        session.commit()
