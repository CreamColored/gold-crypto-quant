"""本地模拟资金账本、权益快照和运行时熔断持久化。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gold_crypto_quant.risk.paper_account import calculate_paper_account
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import (
    PaperAccountState,
    PaperPositionControl,
    Position,
    RuntimeRiskState,
    Trade,
)
from gold_crypto_quant.storage.runtime_risk import (
    evaluate_and_save_runtime_risk,
    save_account_snapshot,
)

PAPER_ACCOUNT_VENUE = "GATE_TESTNET_PAPER"
OANDA_PAPER_ACCOUNT_VENUE = "OANDA_PRACTICE_PAPER"


@dataclass(frozen=True, slots=True)
class PaperAccountRefreshResult:
    """一次账本刷新后的资金和熔断状态。"""

    balance: Decimal
    equity: Decimal
    available_margin: Decimal
    used_margin: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fees: Decimal
    risk_state: str


def refresh_paper_account(
    *,
    now: datetime | None = None,
    initial_equity: Decimal | None = None,
    paper_venue: str = PAPER_ACCOUNT_VENUE,
    engine: Engine | None = None,
) -> PaperAccountRefreshResult | None:
    """从本地成交和持仓重建模拟权益，并刷新2%与8%熔断。"""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("paper account snapshot time must be timezone-aware")
    if initial_equity is not None and initial_equity <= 0:
        raise ValueError("paper initial equity must be positive")
    if not paper_venue:
        raise ValueError("paper venue is required")
    engine = engine or build_engine()
    with Session(engine) as session, session.begin():
        state = session.execute(
            select(PaperAccountState)
            .where(PaperAccountState.venue == paper_venue)
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            if initial_equity is None:
                return None
            # 首次批准策略开仓前调用固定初始权益；后续外部账户变化不会重置模拟本金。
            state = PaperAccountState(
                venue=paper_venue,
                initial_equity=initial_equity,
                balance=initial_equity,
                equity=initial_equity,
                available_margin=initial_equity,
                used_margin=Decimal("0"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                total_fees=Decimal("0"),
            )
            session.add(state)
            session.flush()

        # 调用数据库聚合，退出成交保存毛盈亏，全部入场和平仓手续费只在这里统一扣一次。
        total_fees = session.scalar(
            select(func.coalesce(func.sum(Trade.fee), 0)).where(
                Trade.venue == paper_venue
            )
        )
        realized_pnl = session.scalar(
            select(func.coalesce(func.sum(Trade.realized_pnl), 0)).where(
                Trade.venue == paper_venue,
                Trade.realized_pnl.is_not(None),
            )
        )
        unrealized_pnl = session.scalar(
            select(func.coalesce(func.sum(Position.unrealized_pnl), 0)).where(
                Position.venue == paper_venue,
                Position.quantity > 0,
            )
        )
        positions = session.execute(
            select(Position, PaperPositionControl.contract_multiplier)
            .join(PaperPositionControl, PaperPositionControl.position_id == Position.id)
            .where(
                Position.venue == paper_venue,
                Position.quantity > 0,
                PaperPositionControl.closed_at.is_(None),
            )
        ).all()
        used_margin = Decimal("0")
        for position, multiplier in positions:
            mark_price = position.mark_price or position.entry_price
            # 调用乘数把场所订单数量换成标的数量，再计算名义价值和保证金。
            used_margin += (
                position.quantity * multiplier * mark_price / Decimal(position.leverage)
            )
        evaluation = calculate_paper_account(
            initial_equity=state.initial_equity,
            realized_pnl=Decimal(realized_pnl or 0),
            unrealized_pnl=Decimal(unrealized_pnl or 0),
            total_fees=Decimal(total_fees or 0),
            used_margin=used_margin,
        )
        state.balance = evaluation.balance
        state.equity = evaluation.equity
        state.available_margin = evaluation.available_margin
        state.used_margin = evaluation.used_margin
        state.realized_pnl = evaluation.realized_pnl
        state.unrealized_pnl = evaluation.unrealized_pnl
        state.total_fees = evaluation.total_fees

    # 账本事务完成后调用统一账户快照，UTC日初和历史峰值计算无需复制第二套实现。
    save_account_snapshot(
        venue=paper_venue,
        snapshot_time=now,
        balance=evaluation.balance,
        equity=evaluation.equity,
        available_margin=evaluation.available_margin,
        used_margin=evaluation.used_margin,
        unrealized_pnl=evaluation.unrealized_pnl,
        engine=engine,
    )
    # 调用同一运行时状态机，将每日2%和最大回撤8%应用到本地模拟权益。
    risk = evaluate_and_save_runtime_risk(paper_venue, engine=engine)
    return PaperAccountRefreshResult(
        balance=evaluation.balance,
        equity=evaluation.equity,
        available_margin=evaluation.available_margin,
        used_margin=evaluation.used_margin,
        realized_pnl=evaluation.realized_pnl,
        unrealized_pnl=evaluation.unrealized_pnl,
        total_fees=evaluation.total_fees,
        risk_state=risk.state,
    )


def read_paper_account_state(
    *, paper_venue: str = PAPER_ACCOUNT_VENUE, engine: Engine | None = None
) -> tuple[PaperAccountState | None, RuntimeRiskState | None]:
    """只读返回模拟资金账本及其专属运行时风控状态。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        state = session.execute(
            select(PaperAccountState).where(PaperAccountState.venue == paper_venue)
        ).scalar_one_or_none()
        risk = session.execute(
            select(RuntimeRiskState).where(RuntimeRiskState.venue == paper_venue)
        ).scalar_one_or_none()
        return state, risk
