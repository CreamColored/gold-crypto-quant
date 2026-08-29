"""策略准入决定的MySQL幂等持久化。"""

from datetime import UTC
from decimal import Decimal

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.risk.qualification import QualificationDecision
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, StrategyQualification


def save_qualification(
    decision: QualificationDecision,
    *,
    engine: Engine | None = None,
    venue: str = GATE_TESTNET_VENUE,
) -> int:
    """按评估哈希新增或更新准入审计，并返回数据库记录ID。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        # 调用品种业务键查询，防止准入记录关联到错误市场的同名合约。
        instrument_id = session.execute(
            select(Instrument.id).where(
                Instrument.venue == venue,
                Instrument.symbol == decision.symbol,
            )
        ).scalar_one()
        values = {
            "evaluation_hash": decision.evaluation_hash,
            "instrument_id": instrument_id,
            "strategy_name": decision.strategy_name,
            "strategy_version": decision.strategy_version,
            "interval_code": decision.interval,
            "decision": "APPROVED" if decision.approved else "REJECTED",
            "compounded_return": Decimal(str(decision.compounded_return)),
            "worst_drawdown": Decimal(str(decision.worst_drawdown)),
            "positive_folds": decision.positive_folds,
            "total_folds": decision.total_folds,
            "minimum_fold_trades": decision.minimum_fold_trades,
            "criteria": decision.criteria,
            "details": decision.details,
            "reason": decision.reason,
            # MySQL DATETIME保存无时区UTC数值，项目所有读取方按UTC解释。
            "evaluated_at": decision.evaluated_at.astimezone(UTC).replace(tzinfo=None),
        }
        statement = mysql_insert(StrategyQualification).values(**values)
        # 相同评估哈希重跑时更新审计内容，不新增重复准入决定。
        session.execute(
            statement.on_duplicate_key_update(
                decision=statement.inserted.decision,
                compounded_return=statement.inserted.compounded_return,
                worst_drawdown=statement.inserted.worst_drawdown,
                positive_folds=statement.inserted.positive_folds,
                total_folds=statement.inserted.total_folds,
                minimum_fold_trades=statement.inserted.minimum_fold_trades,
                criteria=statement.inserted.criteria,
                details=statement.inserted.details,
                reason=statement.inserted.reason,
                evaluated_at=statement.inserted.evaluated_at,
            )
        )
        session.commit()
        # 提交后调用唯一哈希查询，兼容首次插入和幂等更新两种路径。
        return session.execute(
            select(StrategyQualification.id).where(
                StrategyQualification.evaluation_hash == decision.evaluation_hash
            )
        ).scalar_one()
