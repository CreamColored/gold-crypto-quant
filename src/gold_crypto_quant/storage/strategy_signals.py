"""EMA策略信号去重保存与准入上下文查询。"""

from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, StrategyQualification, StrategySignal
from gold_crypto_quant.strategy.live_signals import (
    NextBarSignalDecision,
    build_signal_dedupe_key,
)


@dataclass(frozen=True, slots=True)
class QualificationContext:
    """一个品种周期当前可用的最新策略准入结论。"""

    instrument_id: int
    decision: str | None
    evaluation_hash: str | None
    reason: str
    details: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SavedStrategySignal:
    """信号写库结果；created为false表示本次命中已有去重记录。"""

    signal_id: int
    dedupe_key: str
    side: str
    signal_type: str
    created: bool


def read_qualification_context(
    symbol: str,
    interval: str,
    *,
    strategy_name: str = "EMA_TREND",
    strategy_version: str = "1.0.0",
    venue: str = GATE_TESTNET_VENUE,
    engine: Engine | None = None,
) -> QualificationContext:
    """读取品种周期最新准入结论；缺失时明确返回未批准状态。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        # 调用品种唯一业务键查询，禁止同名品种跨交易场所误用审批结果。
        instrument_id = session.execute(
            select(Instrument.id).where(
                Instrument.venue == venue,
                Instrument.symbol == symbol,
            )
        ).scalar_one()
        # 调用时间倒序查询，同一策略重新评估后只采用最新一条审计结论。
        qualification = session.execute(
            select(StrategyQualification)
            .where(
                StrategyQualification.instrument_id == instrument_id,
                StrategyQualification.interval_code == interval,
                StrategyQualification.strategy_name == strategy_name,
                StrategyQualification.strategy_version == strategy_version,
            )
            .order_by(
                StrategyQualification.evaluated_at.desc(),
                StrategyQualification.id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if qualification is None:
            return QualificationContext(
                instrument_id=instrument_id,
                decision=None,
                evaluation_hash=None,
                reason="没有匹配当前品种、周期和策略版本的准入记录",
                details=None,
            )
        return QualificationContext(
            instrument_id=instrument_id,
            decision=qualification.decision,
            evaluation_hash=qualification.evaluation_hash,
            reason=qualification.reason,
            details=qualification.details,
        )


def save_strategy_signals(
    symbol: str,
    interval: str,
    decision: NextBarSignalDecision,
    *,
    strategy_name: str = "EMA_TREND",
    strategy_version: str = "1.0.0",
    strategy_run_id: str | None = None,
    venue: str = GATE_TESTNET_VENUE,
    engine: Engine | None = None,
) -> list[SavedStrategySignal]:
    """按策略、品种、周期、时间和动作幂等保存全部信号。"""
    if not decision.actions:
        return []
    engine = engine or build_engine()
    with Session(engine) as session:
        instrument_id = session.execute(
            select(Instrument.id).where(
                Instrument.venue == venue,
                Instrument.symbol == symbol,
            )
        ).scalar_one()
        keys_by_action = {
            action: build_signal_dedupe_key(
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                instrument_id=instrument_id,
                interval=interval,
                bar_time=decision.bar_time,
                action=action,
            )
            for action in decision.actions
        }
        # 写入前调用批量查询，用于区分本次新信号和幂等重跑命中的旧信号。
        existing_keys = set(
            session.scalars(
                select(StrategySignal.dedupe_key).where(
                    StrategySignal.dedupe_key.in_(keys_by_action.values())
                )
            ).all()
        )
        # MySQL DATETIME不保存时区标记；调用astimezone统一转成UTC数值后再入库。
        database_bar_time = decision.bar_time.astimezone(UTC).replace(tzinfo=None)
        rows = [
            {
                "dedupe_key": dedupe_key,
                "strategy_run_id": strategy_run_id,
                "instrument_id": instrument_id,
                "interval_code": interval,
                "bar_time": database_bar_time,
                "side": action.side,
                "signal_type": action.signal_type,
                "signal_price": decision.signal_price,
                "payload": {
                    **decision.payload,
                    "strategy_name": strategy_name,
                    "strategy_version": strategy_version,
                },
            }
            for action, dedupe_key in keys_by_action.items()
        ]
        statement = mysql_insert(StrategySignal).values(rows)
        # 调用唯一键幂等更新且保持原值，重复轮询绝不会创建第二条相同信号。
        session.execute(
            statement.on_duplicate_key_update(dedupe_key=statement.inserted.dedupe_key)
        )
        session.commit()
        # 提交后调用批量查询取得真实ID，兼容首次写入和重复轮询两种情况。
        saved_rows = session.execute(
            select(
                StrategySignal.id,
                StrategySignal.dedupe_key,
                StrategySignal.side,
                StrategySignal.signal_type,
            ).where(StrategySignal.dedupe_key.in_(keys_by_action.values()))
        ).all()
        return [
            SavedStrategySignal(
                signal_id=row.id,
                dedupe_key=row.dedupe_key,
                side=row.side,
                signal_type=row.signal_type,
                created=row.dedupe_key not in existing_keys,
            )
            for row in saved_rows
        ]
