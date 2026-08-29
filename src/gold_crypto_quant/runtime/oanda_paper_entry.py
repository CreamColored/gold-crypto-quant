"""OANDA黄金获准信号的本地模拟开仓执行器。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from gold_crypto_quant.config import Settings
from gold_crypto_quant.exchanges.oanda import (
    OandaPracticeClient,
    resolve_oanda_account_id,
)
from gold_crypto_quant.execution.orders import OrderIntent
from gold_crypto_quant.market_data.oanda_history import OANDA_PRACTICE_VENUE
from gold_crypto_quant.risk.oanda_paper_order_plan import build_oanda_paper_order_plan
from gold_crypto_quant.risk.paper_order_plan import PaperOrderPlan
from gold_crypto_quant.storage.paper_account import (
    OANDA_PAPER_ACCOUNT_VENUE,
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
class OandaPaperEntryExecution:
    """一条OANDA黄金获准信号的本地模拟成交结果。"""

    order: ReservedPaperOrder
    plan: PaperOrderPlan
    fill: PaperFillResult


def execute_approved_oanda_paper_entry(
    *,
    settings: Settings,
    symbol: str,
    interval: str,
    bars: pd.DataFrame,
    decision: NextBarSignalDecision,
    action: SignalAction,
    qualification: QualificationContext,
    deployment: ApprovedDeploymentParameters,
    primary_interval: str = "30m",
    now: datetime | None = None,
    client_factory: Callable[[], OandaPracticeClient] | None = None,
) -> OandaPaperEntryExecution:
    """用Practice只读数据生成本地成交，绝不向OANDA提交订单。"""
    if action.signal_type != "ENTRY":
        raise ValueError("OANDA paper entry executor only accepts ENTRY actions")
    if symbol != "XAU_USD" or interval != primary_interval:
        raise PermissionError("only the configured XAU_USD primary interval may open positions")
    if qualification.decision != "APPROVED" or qualification.evaluation_hash is None:
        raise PermissionError("strategy qualification is not approved")
    now = now or datetime.now(UTC)
    client_factory = client_factory or (lambda: OandaPracticeClient.from_settings(settings))

    # 调用Practice只读账户、品种规则和报价接口；客户端本身没有任何订单提交方法。
    with client_factory() as client:
        account_id, _auto_selected = resolve_oanda_account_id(
            client, settings.oanda_practice_account_id
        )
        account = client.get_account_summary(account_id)
        rules = client.get_instrument_rules(account_id, symbol)
        price = client.get_price(account_id, symbol)
    if not price.tradeable:
        raise PermissionError("OANDA reports XAU_USD as non-tradeable")

    # 调用真实Practice资金快照仅用于连接和风险基准，不会在该账户创建订单。
    save_account_snapshot(
        venue=OANDA_PRACTICE_VENUE,
        snapshot_time=now,
        balance=account.balance,
        equity=account.nav,
        available_margin=account.margin_available,
        used_margin=account.margin_used,
        unrealized_pnl=account.unrealized_pl,
    )
    # 调用统一2%与8%状态机，保留外部Practice账户本身的独立风控状态。
    evaluate_and_save_runtime_risk(OANDA_PRACTICE_VENUE)
    # 调用OANDA专属模拟账本；首次信号固定本金，后续不会被Practice余额变化重置。
    paper_account = refresh_paper_account(
        now=now,
        initial_equity=account.nav,
        paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
    )
    if paper_account is None:
        raise RuntimeError("OANDA paper account failed to initialize")

    reference_price = price.ask if action.side == "LONG" else price.bid
    # 调用ATR、OANDA单位精度和保证金率换算；实际杠杆不会超过品种允许值。
    plan = build_oanda_paper_order_plan(
        bars,
        side=action.side,
        entry_price=reference_price,
        equity=paper_account.equity,
        available_margin=paper_account.available_margin,
        rules=rules,
        risk_fraction=Decimal(str(settings.risk_per_trade)),
        requested_leverage=settings.leverage,
        atr_multiple=Decimal(str(deployment.atr_multiple)),
    )
    # 调用确定性策略运行创建，同一准入记录的后续信号复用同一个模拟运行。
    strategy_run_id = ensure_paper_strategy_run(
        symbol=symbol,
        interval=interval,
        qualification=qualification,
        deployment=deployment,
        now=now,
    )
    price_tick = Decimal(1).scaleb(-rules.display_precision)
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
            "contract_multiplier": "1",
            "mark_price_at_reservation": str(reference_price),
            "price_tick": str(price_tick),
            # 与黄金研究配置保持一致；这是本地保守成本模型，不代表向OANDA收费。
            "taker_fee_rate": "0.0005",
            "slippage_rate": "0.0002",
            "fee_asset": account.currency,
            "margin_rate": str(rules.margin_rate),
            "signal_price": str(decision.signal_price),
            "source_venue": OANDA_PRACTICE_VENUE,
        },
    )
    # 调用本地预留层，再次检查准入、行情心跳、模拟风控、订单去重和禁止补仓。
    order = reserve_paper_order(
        intent,
        now=now,
        risk_venue=OANDA_PAPER_ACCOUNT_VENUE,
        paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
        quantity_unit="OANDA_UNITS",
    )
    # 调用本地模拟成交；确定性成交编号保证进程重试不会生成第二笔成交。
    fill = fill_created_paper_order(order.order_id, now=now)
    # 入场成本和保证金在后一微秒刷新，保留开仓前的UTC权益基准快照。
    refresh_paper_account(
        now=now + timedelta(microseconds=1),
        paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
    )
    return OandaPaperEntryExecution(order=order, plan=plan, fill=fill)
