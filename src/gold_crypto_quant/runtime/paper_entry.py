"""已批准EMA信号的账户刷新、仓位计划与本地订单预留。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from gold_crypto_quant.config import Settings
from gold_crypto_quant.exchanges.gate import GateTestnetClient
from gold_crypto_quant.execution.orders import OrderIntent
from gold_crypto_quant.risk.paper_order_plan import PaperOrderPlan, build_paper_order_plan
from gold_crypto_quant.storage.paper_account import (
    PAPER_ACCOUNT_VENUE,
    refresh_paper_account,
)
from gold_crypto_quant.storage.paper_fills import PaperFillResult, fill_created_paper_order
from gold_crypto_quant.storage.paper_orders import ReservedPaperOrder, reserve_paper_order
from gold_crypto_quant.storage.runtime_risk import (
    evaluate_and_save_runtime_risk,
    save_account_snapshot,
)
from gold_crypto_quant.storage.strategy_runs import ensure_paper_strategy_run
from gold_crypto_quant.storage.strategy_signals import QualificationContext
from gold_crypto_quant.strategy.deployment import ApprovedDeploymentParameters
from gold_crypto_quant.strategy.live_signals import NextBarSignalDecision, SignalAction


@dataclass(frozen=True, slots=True)
class PaperEntryExecution:
    """一条已通过全部本地门禁的模拟开仓预留结果。"""

    order: ReservedPaperOrder
    plan: PaperOrderPlan
    fill: PaperFillResult


def execute_approved_paper_entry(
    *,
    settings: Settings,
    symbol: str,
    interval: str,
    bars: pd.DataFrame,
    decision: NextBarSignalDecision,
    action: SignalAction,
    qualification: QualificationContext,
    deployment: ApprovedDeploymentParameters,
    now: datetime | None = None,
    client_factory: Callable[[], GateTestnetClient] | None = None,
) -> PaperEntryExecution:
    """刷新测试网账户后构造并预留本地CREATED订单，绝不提交交易所。"""
    if action.signal_type != "ENTRY":
        raise ValueError("paper entry executor only accepts ENTRY actions")
    if qualification.decision != "APPROVED" or qualification.evaluation_hash is None:
        raise PermissionError("strategy qualification is not approved")
    now = now or datetime.now(UTC)
    client_factory = client_factory or (lambda: GateTestnetClient.from_settings(settings))
    # 调用测试网公开合约和私有账户查询；客户端没有下单方法，with结束后立即断开。
    with client_factory() as client:
        contract = client.get_contract(symbol)
        account = client.get_account()
    snapshot_time = now
    balance = account.total - account.unrealised_pnl
    used_margin = account.position_margin + account.order_margin
    # 调用资金快照保存，确保本次订单使用的是刚读取的测试网权益和可用保证金。
    save_account_snapshot(
        venue="GATE_TESTNET",
        snapshot_time=snapshot_time,
        balance=balance,
        equity=account.total,
        available_margin=account.available,
        used_margin=used_margin,
        unrealized_pnl=account.unrealised_pnl,
    )
    # 调用2%每日亏损和8%最大回撤状态机；非NORMAL会在订单预留层再次拒绝。
    evaluate_and_save_runtime_risk("GATE_TESTNET")
    # 调用模拟资金账本固定初始本金，并发布本地权益专属的2%与8%熔断状态。
    paper_account = refresh_paper_account(now=now, initial_equity=account.total)
    if paper_account is None:
        raise RuntimeError("paper account failed to initialize")
    # 调用ATR与Gate张数换算，实际杠杆取配置上限和交易所品种上限的较小值。
    plan = build_paper_order_plan(
        bars,
        side=action.side,
        entry_price=contract.mark_price,
        equity=account.total,
        available_margin=account.available,
        contract=contract,
        risk_fraction=Decimal(str(settings.risk_per_trade)),
        requested_leverage=settings.leverage,
        atr_multiple=Decimal(str(deployment.atr_multiple)),
    )
    # 调用确定性策略运行创建；同一批准记录的后续信号复用同一个30天模拟运行。
    strategy_run_id = ensure_paper_strategy_run(
        symbol=symbol,
        interval=interval,
        qualification=qualification,
        deployment=deployment,
        now=now,
    )
    intent = OrderIntent(
        strategy_run_id=strategy_run_id,
        qualification_hash=qualification.evaluation_hash,
        instrument_id=qualification.instrument_id,
        symbol=symbol,
        interval=interval,
        bar_time=decision.bar_time,
        side="BUY" if action.side == "LONG" else "SELL",
        position_side=action.side,
        quantity=plan.contract_quantity,
        leverage=plan.leverage,
        price=None,
        stop_price=plan.stop_price,
        metadata={
            "contract_multiplier": str(contract.quanto_multiplier),
            "mark_price_at_reservation": str(contract.mark_price),
            "price_tick": str(contract.order_price_round),
            "taker_fee_rate": str(contract.taker_fee_rate),
            "signal_price": str(decision.signal_price),
            "slippage_rate": "0.0002",
        },
    )
    # 调用本地订单预留会再次检查准入、账户风控、行情心跳、禁止补仓和订单去重。
    order = reserve_paper_order(intent, now=now, risk_venue=PAPER_ACCOUNT_VENUE)
    # 预留成功后调用本地模拟成交；确定性成交号保证崩溃重试不会产生第二笔成交。
    fill = fill_created_paper_order(order.order_id, now=now)
    # 入场手续费和占用保证金在后一微秒调用账本刷新，保留开仓前日初权益快照。
    refresh_paper_account(now=now + timedelta(microseconds=1))
    return PaperEntryExecution(order=order, plan=plan, fill=fill)
