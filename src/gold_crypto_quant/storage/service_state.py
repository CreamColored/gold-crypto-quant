"""长期运行服务状态的MySQL持久化方法。"""

from datetime import UTC, datetime

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import ServiceHeartbeat, ServiceRuntimeState


def save_service_state(
    *,
    service_name: str,
    status: str,
    process_id: int,
    started_at: datetime,
    heartbeat_at: datetime,
    successful_cycles: int,
    consecutive_failures: int,
    stopped_at: datetime | None = None,
    last_error: str | None = None,
    details: dict[str, object] | None = None,
    engine: Engine | None = None,
) -> ServiceRuntimeState:
    """按服务名称幂等保存进程状态，重复心跳不会新增记录。"""
    timestamps = (started_at, heartbeat_at, stopped_at)
    if any(value is not None and value.tzinfo is None for value in timestamps):
        raise ValueError("service state timestamps must be timezone-aware")
    if successful_cycles < 0 or consecutive_failures < 0:
        raise ValueError("service state counters cannot be negative")

    def database_time(value: datetime | None) -> datetime | None:
        """把带时区时间转换为MySQL约定的无时区UTC数值。"""
        return value.astimezone(UTC).replace(tzinfo=None) if value is not None else None

    engine = engine or build_engine()
    statement = mysql_insert(ServiceRuntimeState).values(
        service_name=service_name,
        status=status,
        process_id=process_id,
        started_at=database_time(started_at),
        heartbeat_at=database_time(heartbeat_at),
        stopped_at=database_time(stopped_at),
        successful_cycles=successful_cycles,
        consecutive_failures=consecutive_failures,
        last_error=last_error,
        details=details,
    )
    with Session(engine) as session:
        # 调用MySQL幂等更新，同一个服务始终只保留一条最新运行状态。
        session.execute(
            statement.on_duplicate_key_update(
                status=statement.inserted.status,
                process_id=statement.inserted.process_id,
                started_at=statement.inserted.started_at,
                heartbeat_at=statement.inserted.heartbeat_at,
                stopped_at=statement.inserted.stopped_at,
                successful_cycles=statement.inserted.successful_cycles,
                consecutive_failures=statement.inserted.consecutive_failures,
                last_error=statement.inserted.last_error,
                details=statement.inserted.details,
            )
        )
        # 追加本次心跳历史；与最新状态在同一事务提交，便于审计断线和连续运行天数。
        session.add(
            ServiceHeartbeat(
                service_name=service_name,
                status=status,
                process_id=process_id,
                recorded_at=database_time(heartbeat_at),
                successful_cycles=successful_cycles,
                consecutive_failures=consecutive_failures,
                details={**(details or {}), "last_error": last_error},
            )
        )
        session.commit()
        # 提交后调用唯一服务名查询，返回数据库实际保存的状态。
        return session.execute(
            select(ServiceRuntimeState).where(ServiceRuntimeState.service_name == service_name)
        ).scalar_one()


def read_service_state(
    service_name: str, *, engine: Engine | None = None
) -> ServiceRuntimeState | None:
    """读取指定长期服务的最近状态，不修改数据库。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        # 调用单行查询；服务从未启动时返回None而不是制造默认健康状态。
        return session.execute(
            select(ServiceRuntimeState).where(ServiceRuntimeState.service_name == service_name)
        ).scalar_one_or_none()
