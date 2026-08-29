"""已批准策略的长期模拟运行记录。"""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import StrategyRun
from gold_crypto_quant.storage.strategy_signals import QualificationContext
from gold_crypto_quant.strategy.deployment import ApprovedDeploymentParameters


def ensure_paper_strategy_run(
    *,
    symbol: str,
    interval: str,
    qualification: QualificationContext,
    deployment: ApprovedDeploymentParameters,
    now: datetime,
    engine: Engine | None = None,
) -> str:
    """按准入哈希创建唯一30天模拟运行，重复信号复用原运行记录。"""
    if now.tzinfo is None:
        raise ValueError("strategy run time must be timezone-aware")
    if qualification.decision != "APPROVED" or qualification.evaluation_hash is None:
        raise PermissionError("approved qualification hash is required for paper run")
    run_id = str(
        uuid5(
            NAMESPACE_URL,
            f"gold-crypto-quant|paper|{qualification.evaluation_hash}",
        )
    )
    engine = engine or build_engine()
    parameters = {
        "symbol": symbol,
        "qualification_hash": qualification.evaluation_hash,
        "ema_fast": deployment.strategy.fast_period,
        "ema_slow": deployment.strategy.slow_period,
        "ema_trend": deployment.strategy.trend_period,
        "min_trend_strength": deployment.strategy.min_trend_strength,
        "allow_long": deployment.strategy.allow_long,
        "allow_short": deployment.strategy.allow_short,
        "min_adx": deployment.strategy.min_adx,
        "higher_timeframe_filter": deployment.strategy.use_higher_timeframe_filter,
        "higher_timeframe_mode": deployment.strategy.higher_timeframe_mode,
        "cooldown_bars": deployment.strategy.cooldown_bars,
        "entry_mode": deployment.strategy.entry_mode,
        "pullback_lookback": deployment.strategy.pullback_lookback,
        "atr_multiple": deployment.atr_multiple,
        "minimum_stable_days": 30,
        "live_trading": False,
    }
    database_now = now.astimezone(UTC).replace(tzinfo=None)
    with Session(engine) as session:
        existing = session.execute(
            select(StrategyRun).where(StrategyRun.id == run_id)
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status != "PAPER_RUNNING":
                raise PermissionError(f"paper strategy run is not active: {existing.status}")
            return existing.id
        statement = mysql_insert(StrategyRun).values(
            id=run_id,
            strategy_name="EMA_TREND",
            strategy_version="1.0.0",
            interval_code=interval,
            status="PAPER_RUNNING",
            parameters=parameters,
            started_at=database_now,
            ended_at=None,
        )
        # 调用准入哈希派生的确定ID插入；并发启动也只会保留一条运行记录。
        session.execute(statement.on_duplicate_key_update(id=statement.inserted.id))
        session.commit()
        return run_id
