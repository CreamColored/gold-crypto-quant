"""行情流健康状态的MySQL检查与持久化。"""

from datetime import UTC, datetime

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.risk.market_health import MarketHealthStatus, evaluate_market_health
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import (
    Instrument,
    MarketBar,
    MarketDataHealthState,
    RiskEvent,
)


def refresh_market_health(
    symbol: str,
    interval: str,
    *,
    venue: str = GATE_TESTNET_VENUE,
    now: datetime | None = None,
    engine: Engine | None = None,
) -> MarketDataHealthState:
    """检查一个行情流并幂等保存心跳和数据新鲜度。"""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    engine = engine or build_engine()
    with Session(engine) as session:
        instrument_id = session.execute(
            select(Instrument.id).where(
                Instrument.venue == venue,
                Instrument.symbol == symbol,
            )
        ).scalar_one()
        latest_close = session.execute(
            select(MarketBar.close_time)
            .where(
                MarketBar.instrument_id == instrument_id,
                MarketBar.interval_code == interval,
                MarketBar.is_closed.is_(True),
            )
            # 按 open_time 排而不是 close_time：索引是
            # (instrument_id, interval_code, open_time)，按 close_time 排会退化成
            # filesort，每次排三千多行。同一周期内 close_time = open_time + 周期长度，
            # 严格同序，取到的是同一根K线。
            .order_by(MarketBar.open_time.desc())
            .limit(1)
        ).scalar_one_or_none()
        aware_latest_close = latest_close.replace(tzinfo=UTC) if latest_close is not None else None
        # 新心跳自身就是当前检查时间，因此这里只评估K线缺失、未来时间和过期。
        evaluation = evaluate_market_health(
            now=now,
            interval=interval,
            last_bar_close_time=aware_latest_close,
        )
        previous = session.execute(
            select(MarketDataHealthState)
            .where(
                MarketDataHealthState.instrument_id == instrument_id,
                MarketDataHealthState.interval_code == interval,
            )
            .with_for_update()
        ).scalar_one_or_none()
        previous_status = previous.status if previous is not None else None
        failures = (
            0
            if evaluation.status == MarketHealthStatus.HEALTHY
            else (previous.consecutive_failures if previous is not None else 0) + 1
        )
        database_now = now.astimezone(UTC).replace(tzinfo=None)
        statement = mysql_insert(MarketDataHealthState).values(
            instrument_id=instrument_id,
            interval_code=interval,
            status=evaluation.status.value,
            heartbeat_at=database_now,
            last_bar_close_time=latest_close,
            stale_seconds=evaluation.stale_seconds,
            consecutive_failures=failures,
            reason=evaluation.reason,
            details={"symbol": symbol, "venue": venue},
        )
        session.execute(
            statement.on_duplicate_key_update(
                status=statement.inserted.status,
                heartbeat_at=statement.inserted.heartbeat_at,
                last_bar_close_time=statement.inserted.last_bar_close_time,
                stale_seconds=statement.inserted.stale_seconds,
                consecutive_failures=statement.inserted.consecutive_failures,
                reason=statement.inserted.reason,
                details=statement.inserted.details,
            )
        )
        if previous_status != evaluation.status.value:
            # 只有状态转换才调用RiskEvent记录，重复STALE检查不会产生告警风暴。
            session.add(
                RiskEvent(
                    instrument_id=instrument_id,
                    event_time=database_now,
                    severity=(
                        "INFO" if evaluation.status == MarketHealthStatus.HEALTHY else "CRITICAL"
                    ),
                    event_type=f"MARKET_DATA_{evaluation.status.value}",
                    reason=evaluation.reason,
                    details={
                        "interval": interval,
                        "stale_seconds": evaluation.stale_seconds,
                        "previous_status": previous_status,
                    },
                    is_resolved=evaluation.status == MarketHealthStatus.HEALTHY,
                )
            )
        session.commit()
        # 提交后调用唯一业务键查询并返回实际保存状态。
        return session.execute(
            select(MarketDataHealthState).where(
                MarketDataHealthState.instrument_id == instrument_id,
                MarketDataHealthState.interval_code == interval,
            )
        ).scalar_one()


def require_market_health_for_order(
    session: Session,
    *,
    instrument_id: int,
    interval: str,
    signal_time: datetime,
    now: datetime,
) -> None:
    """订单预留前重新验证持久化心跳仍然新鲜且覆盖信号时间。"""
    state = session.execute(
        select(MarketDataHealthState).where(
            MarketDataHealthState.instrument_id == instrument_id,
            MarketDataHealthState.interval_code == interval,
        )
    ).scalar_one_or_none()
    if state is None:
        raise PermissionError("market data health state is missing")
    heartbeat_at = state.heartbeat_at.replace(tzinfo=UTC)
    last_bar_close = (
        state.last_bar_close_time.replace(tzinfo=UTC)
        if state.last_bar_close_time is not None
        else None
    )
    # 调用纯健康状态机，防止数据库中曾经HEALTHY的旧心跳被长期复用。
    evaluation = evaluate_market_health(
        now=now,
        interval=interval,
        last_bar_close_time=last_bar_close,
        last_heartbeat_at=heartbeat_at,
    )
    if evaluation.status != MarketHealthStatus.HEALTHY:
        raise PermissionError(f"market data health blocks orders: {evaluation.status.value}")
    if last_bar_close is None or last_bar_close < signal_time:
        raise PermissionError("market data does not cover the order signal time")
