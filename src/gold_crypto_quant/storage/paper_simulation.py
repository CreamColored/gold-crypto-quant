"""30天模拟运行监督器的MySQL汇总和状态读取。"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.risk.paper_simulation import (
    PaperSimulationDay,
    evaluate_paper_simulation,
)
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import (
    AccountSnapshot,
    PaperAccountState,
    PaperSimulationDailyMetric,
    PaperSimulationState,
    ServiceHeartbeat,
    Trade,
)
from gold_crypto_quant.storage.paper_account import PAPER_ACCOUNT_VENUE

REQUIRED_HEALTHY_DAYS = 30
MAXIMUM_HEARTBEAT_GAP_SECONDS = 180
MARKET_DATA_SERVICE_NAME = "GATE_TESTNET_MARKET_DATA"


def _utc_day_bounds(trading_day: date) -> tuple[datetime, datetime]:
    """返回MySQL使用的无时区UTC日边界。"""
    start = datetime.combine(trading_day, time.min)
    return start, start + timedelta(days=1)


def _maximum_gap_seconds(
    heartbeat_times: list[datetime], day_start: datetime, day_end: datetime
) -> int:
    """把UTC日首尾边界也纳入心跳缺口，避免只在日中短暂启动被判健康。"""
    boundaries = [day_start, *sorted(heartbeat_times), day_end]
    return max(
        int((right - left).total_seconds())
        for left, right in zip(boundaries, boundaries[1:], strict=False)
    )


def refresh_paper_simulation(
    *,
    now: datetime | None = None,
    paper_venue: str = PAPER_ACCOUNT_VENUE,
    market_service_name: str = MARKET_DATA_SERVICE_NAME,
    engine: Engine | None = None,
) -> PaperSimulationState | None:
    """刷新完整UTC日指标；模拟账户未初始化时不启动30天计时。"""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("paper simulation evaluation time must be timezone-aware")
    database_now = now.astimezone(UTC).replace(tzinfo=None)
    engine = engine or build_engine()

    with Session(engine) as session:
        account = session.execute(
            select(PaperAccountState).where(PaperAccountState.venue == paper_venue)
        ).scalar_one_or_none()
        if account is None:
            return None

        snapshots = list(
            session.scalars(
                select(AccountSnapshot)
                .where(AccountSnapshot.venue == paper_venue)
                .order_by(AccountSnapshot.snapshot_time)
            )
        )
        if not snapshots:
            return None
        started_at = snapshots[0].snapshot_time
        first_full_day = started_at.date() + timedelta(days=1)
        last_complete_day = database_now.date() - timedelta(days=1)
        observations: list[PaperSimulationDay] = []
        running_peak = max(
            [Decimal(account.initial_equity)]
            + [
                Decimal(item.equity)
                for item in snapshots
                if item.snapshot_time < datetime.combine(first_full_day, time.min)
            ]
        )

        trading_day = first_full_day
        while trading_day <= last_complete_day:
            day_start, day_end = _utc_day_bounds(trading_day)
            prior_snapshots = [item for item in snapshots if item.snapshot_time < day_start]
            daily_snapshots = [
                item for item in snapshots if day_start <= item.snapshot_time < day_end
            ]
            day_start_equity = Decimal(
                prior_snapshots[-1].equity if prior_snapshots else account.initial_equity
            )
            day_end_equity = Decimal(
                daily_snapshots[-1].equity if daily_snapshots else day_start_equity
            )
            daily_return = day_end_equity / day_start_equity - Decimal("1")

            drawdown = Decimal("0")
            for snapshot in daily_snapshots:
                equity = Decimal(snapshot.equity)
                running_peak = max(running_peak, equity)
                drawdown = min(drawdown, equity / running_peak - Decimal("1"))

            heartbeats = list(
                session.scalars(
                    select(ServiceHeartbeat)
                    .where(
                        ServiceHeartbeat.service_name == market_service_name,
                        ServiceHeartbeat.recorded_at >= day_start,
                        ServiceHeartbeat.recorded_at < day_end,
                    )
                    .order_by(ServiceHeartbeat.recorded_at)
                )
            )
            heartbeat_times = [heartbeat.recorded_at for heartbeat in heartbeats]
            maximum_gap = _maximum_gap_seconds(heartbeat_times, day_start, day_end)
            retry_count = sum(heartbeat.status == "RETRYING" for heartbeat in heartbeats)
            trade_count = session.scalar(
                select(func.count(Trade.id)).where(
                    Trade.venue == paper_venue,
                    Trade.executed_at >= day_start,
                    Trade.executed_at < day_end,
                )
            ) or 0

            if drawdown <= Decimal("-0.08"):
                risk_state = "DRAWDOWN_HALTED"
            elif daily_return <= Decimal("-0.02"):
                risk_state = "DAILY_HALTED"
            else:
                risk_state = "NORMAL"
            is_healthy = bool(
                daily_snapshots
                and heartbeats
                and retry_count == 0
                and maximum_gap <= MAXIMUM_HEARTBEAT_GAP_SECONDS
                and risk_state == "NORMAL"
            )
            if not daily_snapshots:
                reason = "缺少模拟权益快照"
            elif not heartbeats:
                reason = "缺少行情服务心跳"
            elif retry_count:
                reason = f"行情服务发生{retry_count}次重试"
            elif maximum_gap > MAXIMUM_HEARTBEAT_GAP_SECONDS:
                reason = f"最大心跳间隔{maximum_gap}秒超过180秒"
            elif risk_state != "NORMAL":
                reason = f"账户风控状态为{risk_state}"
            else:
                reason = "权益、回撤和行情服务连续性均正常"

            statement = mysql_insert(PaperSimulationDailyMetric).values(
                venue=paper_venue,
                trading_day=trading_day,
                day_start_equity=day_start_equity,
                day_end_equity=day_end_equity,
                daily_return=daily_return,
                maximum_drawdown=drawdown,
                heartbeat_count=len(heartbeats),
                retry_count=retry_count,
                maximum_heartbeat_gap_seconds=maximum_gap,
                trade_count=trade_count,
                risk_state=risk_state,
                is_complete=True,
                is_healthy=is_healthy,
                reason=reason,
            )
            # 调用MySQL唯一键更新，让同一天可安全重复汇总而不会生成重复记录。
            session.execute(
                statement.on_duplicate_key_update(
                    day_start_equity=statement.inserted.day_start_equity,
                    day_end_equity=statement.inserted.day_end_equity,
                    daily_return=statement.inserted.daily_return,
                    maximum_drawdown=statement.inserted.maximum_drawdown,
                    heartbeat_count=statement.inserted.heartbeat_count,
                    retry_count=statement.inserted.retry_count,
                    maximum_heartbeat_gap_seconds=statement.inserted.maximum_heartbeat_gap_seconds,
                    trade_count=statement.inserted.trade_count,
                    risk_state=statement.inserted.risk_state,
                    is_complete=statement.inserted.is_complete,
                    is_healthy=statement.inserted.is_healthy,
                    reason=statement.inserted.reason,
                )
            )
            observations.append(
                PaperSimulationDay(
                    trading_day=trading_day,
                    is_complete=True,
                    is_healthy=is_healthy,
                    retry_count=retry_count,
                    maximum_drawdown=drawdown,
                    risk_state=risk_state,
                )
            )
            trading_day += timedelta(days=1)

        # 调用纯监督规则，数据库层不自行决定30天是否达标。
        evaluation = evaluate_paper_simulation(observations)
        current_equity = Decimal(account.equity)
        total_return = current_equity / Decimal(account.initial_equity) - Decimal("1")
        state_statement = mysql_insert(PaperSimulationState).values(
            venue=paper_venue,
            status=evaluation.status.value,
            started_at=started_at,
            required_days=REQUIRED_HEALTHY_DAYS,
            elapsed_days=evaluation.elapsed_days,
            consecutive_healthy_days=evaluation.consecutive_healthy_days,
            current_equity=current_equity,
            total_return=total_return,
            maximum_drawdown=evaluation.maximum_drawdown,
            service_incident_count=evaluation.service_incident_count,
            reason=evaluation.reason,
            last_evaluated_at=database_now,
        )
        # 调用venue唯一键更新监督总状态；该表不包含任何LIVE_TRADING开关。
        session.execute(
            state_statement.on_duplicate_key_update(
                status=state_statement.inserted.status,
                started_at=state_statement.inserted.started_at,
                required_days=state_statement.inserted.required_days,
                elapsed_days=state_statement.inserted.elapsed_days,
                consecutive_healthy_days=state_statement.inserted.consecutive_healthy_days,
                current_equity=state_statement.inserted.current_equity,
                total_return=state_statement.inserted.total_return,
                maximum_drawdown=state_statement.inserted.maximum_drawdown,
                service_incident_count=state_statement.inserted.service_incident_count,
                reason=state_statement.inserted.reason,
                last_evaluated_at=state_statement.inserted.last_evaluated_at,
            )
        )
        session.commit()
        # 提交后调用唯一venue查询，返回数据库实际保存的监督状态。
        return session.execute(
            select(PaperSimulationState).where(PaperSimulationState.venue == paper_venue)
        ).scalar_one()


def read_paper_simulation_state(
    *, paper_venue: str = PAPER_ACCOUNT_VENUE, engine: Engine | None = None
) -> PaperSimulationState | None:
    """只读返回30天模拟监督状态，未开始时返回None。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        # 调用venue唯一键查询，不存在时明确保留NOT_STARTED语义。
        return session.execute(
            select(PaperSimulationState).where(PaperSimulationState.venue == paper_venue)
        ).scalar_one_or_none()
