"""账户快照和运行时风控状态的MySQL持久化。"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.config import get_settings
from gold_crypto_quant.risk.runtime import RuntimeRiskStatus, evaluate_runtime_risk
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import AccountSnapshot, RiskEvent, RuntimeRiskState


def save_account_snapshot(
    *,
    venue: str,
    snapshot_time: datetime,
    balance: Decimal,
    equity: Decimal,
    available_margin: Decimal,
    used_margin: Decimal,
    unrealized_pnl: Decimal,
    engine: Engine | None = None,
) -> int:
    """按交易场所和时间幂等保存账户权益快照。"""
    if snapshot_time.tzinfo is None:
        raise ValueError("snapshot_time must be timezone-aware")
    engine = engine or build_engine()
    database_time = snapshot_time.astimezone(UTC).replace(tzinfo=None)
    with Session(engine) as session:
        statement = mysql_insert(AccountSnapshot).values(
            venue=venue,
            snapshot_time=database_time,
            balance=balance,
            equity=equity,
            available_margin=available_margin,
            used_margin=used_margin,
            unrealized_pnl=unrealized_pnl,
        )
        # 同一微秒的重复采集只刷新数值，不产生重复快照。
        session.execute(
            statement.on_duplicate_key_update(
                balance=statement.inserted.balance,
                equity=statement.inserted.equity,
                available_margin=statement.inserted.available_margin,
                used_margin=statement.inserted.used_margin,
                unrealized_pnl=statement.inserted.unrealized_pnl,
            )
        )
        session.commit()
        # 调用唯一业务键查询，兼容首次插入和幂等更新两种路径。
        return session.execute(
            select(AccountSnapshot.id).where(
                AccountSnapshot.venue == venue,
                AccountSnapshot.snapshot_time == database_time,
            )
        ).scalar_one()


def evaluate_and_save_runtime_risk(
    venue: str,
    *,
    engine: Engine | None = None,
) -> RuntimeRiskState:
    """使用最新账户快照计算并持久化账户风控状态。"""
    engine = engine or build_engine()
    settings = get_settings()
    with Session(engine) as session:
        latest = session.execute(
            select(AccountSnapshot)
            .where(AccountSnapshot.venue == venue)
            .order_by(AccountSnapshot.snapshot_time.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            raise RuntimeError(f"no account snapshot found for {venue}")

        trading_day = latest.snapshot_time.date()
        day_start = datetime.combine(trading_day, datetime.min.time())
        previous_close_equity = session.execute(
            select(AccountSnapshot.equity)
            .where(
                AccountSnapshot.venue == venue,
                AccountSnapshot.snapshot_time < day_start,
            )
            .order_by(AccountSnapshot.snapshot_time.desc())
            .limit(1)
        ).scalar_one_or_none()
        if previous_close_equity is None:
            # 当天没有前收快照时，调用当天最早快照作为日初权益基准。
            previous_close_equity = session.execute(
                select(AccountSnapshot.equity)
                .where(
                    AccountSnapshot.venue == venue,
                    AccountSnapshot.snapshot_time >= day_start,
                )
                .order_by(AccountSnapshot.snapshot_time)
                .limit(1)
            ).scalar_one()
        historical_peak = session.scalar(
            select(func.max(AccountSnapshot.equity)).where(
                AccountSnapshot.venue == venue,
                AccountSnapshot.snapshot_time <= latest.snapshot_time,
            )
        )
        previous = session.execute(
            select(RuntimeRiskState).where(RuntimeRiskState.venue == venue).with_for_update()
        ).scalar_one_or_none()

        # 调用纯状态机，数据库层只负责提供日初、峰值和上一状态。
        evaluation = evaluate_runtime_risk(
            trading_day=trading_day,
            current_equity=latest.equity,
            day_start_equity=previous_close_equity,
            historical_peak_equity=historical_peak,
            daily_loss_limit=Decimal(str(settings.daily_loss_limit)),
            max_drawdown_limit=Decimal(str(settings.max_drawdown_limit)),
            previous_state=RuntimeRiskStatus(previous.state) if previous is not None else None,
            previous_trading_day=previous.trading_day if previous is not None else None,
        )
        previous_status = previous.state if previous is not None else None
        statement = mysql_insert(RuntimeRiskState).values(
            venue=venue,
            state=evaluation.state.value,
            trading_day=evaluation.trading_day,
            day_start_equity=evaluation.day_start_equity,
            peak_equity=evaluation.peak_equity,
            latest_equity=evaluation.latest_equity,
            daily_return=evaluation.daily_return,
            drawdown=evaluation.drawdown,
            reason=evaluation.reason,
        )
        session.execute(
            statement.on_duplicate_key_update(
                state=statement.inserted.state,
                trading_day=statement.inserted.trading_day,
                day_start_equity=statement.inserted.day_start_equity,
                peak_equity=statement.inserted.peak_equity,
                latest_equity=statement.inserted.latest_equity,
                daily_return=statement.inserted.daily_return,
                drawdown=statement.inserted.drawdown,
                reason=statement.inserted.reason,
            )
        )

        if previous_status != evaluation.state.value:
            event_type = {
                RuntimeRiskStatus.NORMAL: "RISK_STATE_NORMAL",
                RuntimeRiskStatus.DAILY_HALTED: "DAILY_LOSS_HALT",
                RuntimeRiskStatus.DRAWDOWN_HALTED: "MAX_DRAWDOWN_HALT",
            }[evaluation.state]
            # 状态只在发生转换时写一条事件；同状态重复快照不会制造重复告警。
            session.add(
                RiskEvent(
                    instrument_id=None,
                    event_time=latest.snapshot_time,
                    severity="INFO" if evaluation.state == RuntimeRiskStatus.NORMAL else "CRITICAL",
                    event_type=event_type,
                    reason=evaluation.reason,
                    details={
                        "venue": venue,
                        "daily_return": str(evaluation.daily_return),
                        "drawdown": str(evaluation.drawdown),
                        "previous_state": previous_status,
                    },
                    is_resolved=evaluation.state == RuntimeRiskStatus.NORMAL,
                )
            )
        session.commit()
        # 提交后调用唯一venue查询，返回数据库实际保存的最新状态。
        return session.execute(
            select(RuntimeRiskState).where(RuntimeRiskState.venue == venue)
        ).scalar_one()
