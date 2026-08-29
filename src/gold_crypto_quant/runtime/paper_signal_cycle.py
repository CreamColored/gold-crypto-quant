"""模拟交易前的EMA信号观察单轮主循环。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.market_health import refresh_market_health
from gold_crypto_quant.storage.strategy_signals import (
    QualificationContext,
    SavedStrategySignal,
    read_qualification_context,
    save_strategy_signals,
)
from gold_crypto_quant.strategy.deployment import (
    ApprovedDeploymentParameters,
    resolve_approved_parameters,
)
from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters
from gold_crypto_quant.strategy.live_signals import (
    NextBarSignalDecision,
    evaluate_next_bar_signal,
)


@dataclass(frozen=True, slots=True)
class StreamSignalResult:
    """一个品种周期在本轮信号观察中的处理结果。"""

    symbol: str
    interval: str
    status: str
    action_count: int
    new_signal_count: int
    order_count: int
    reason: str


@dataclass(frozen=True, slots=True)
class PaperSignalCycleSummary:
    """全部行情流的信号和订单门禁汇总。"""

    streams: tuple[StreamSignalResult, ...]

    @property
    def new_signal_count(self) -> int:
        return sum(stream.new_signal_count for stream in self.streams)

    @property
    def order_count(self) -> int:
        return sum(stream.order_count for stream in self.streams)


def require_approved_entry_qualification(context: QualificationContext) -> None:
    """开仓处理前要求当前策略版本存在明确APPROVED准入记录。"""
    if context.decision is None:
        raise PermissionError("strategy qualification is missing")
    if context.decision != "APPROVED":
        raise PermissionError(f"strategy qualification blocks entries: {context.decision}")


def run_paper_signal_cycle(
    contracts: tuple[str, ...],
    intervals: tuple[str, ...],
    *,
    bar_limit: int = 500,
    health_refresher: Callable[..., Any] = refresh_market_health,
    bar_loader: Callable[..., pd.DataFrame] = load_market_bars,
    signal_evaluator: Callable[..., NextBarSignalDecision] = evaluate_next_bar_signal,
    signal_saver: Callable[..., list[SavedStrategySignal]] = save_strategy_signals,
    qualification_reader: Callable[..., QualificationContext] = read_qualification_context,
    entry_executor: Callable[..., Any] | None = None,
    exit_executor: Callable[..., Any] | None = None,
    position_monitor: Callable[[], Any] | None = None,
    venue: str = GATE_TESTNET_VENUE,
) -> PaperSignalCycleSummary:
    """检查健康行情、计算下一根信号并在准入失败时关闭订单路径。"""
    if bar_limit < 202:
        raise ValueError("paper signal cycle requires at least 202 bars")
    if position_monitor is not None:
        # 在生成新信号前调用持仓监控，保护止损和平仓永远优先于新开仓。
        position_monitor()
    stream_results: list[StreamSignalResult] = []
    for contract in contracts:
        for interval in intervals:
            try:
                # 调用行情健康刷新，异常和非HEALTHY状态都不允许生成可执行信号。
                health = (
                    health_refresher(contract, interval)
                    if venue == GATE_TESTNET_VENUE
                    else health_refresher(contract, interval, venue=venue)
                )
            except Exception as error:
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="BLOCKED_DATA_ERROR",
                        action_count=0,
                        new_signal_count=0,
                        order_count=0,
                        reason=f"行情健康检查失败：{type(error).__name__}: {error}",
                    )
                )
                continue
            if health.status != "HEALTHY":
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="BLOCKED_MARKET_HEALTH",
                        action_count=0,
                        new_signal_count=0,
                        order_count=0,
                        reason=health.reason,
                    )
                )
                continue
            try:
                # 调用最新准入查询；APPROVED时必须使用审计记录中的稳定参数生成信号。
                qualification = (
                    qualification_reader(contract, interval)
                    if venue == GATE_TESTNET_VENUE
                    else qualification_reader(contract, interval, venue=venue)
                )
                deployment = (
                    resolve_approved_parameters(qualification)
                    if qualification.decision == "APPROVED"
                    else ApprovedDeploymentParameters(
                        strategy=EmaTrendParameters(),
                        atr_multiple=1.5,
                    )
                )
            except Exception as error:
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="BLOCKED_QUALIFICATION",
                        action_count=0,
                        new_signal_count=0,
                        order_count=0,
                        reason=f"准入参数读取失败：{type(error).__name__}: {error}",
                    )
                )
                continue
            try:
                # 调用最近窗口读取，500根足够EMA200预热且避免每分钟加载全部历史数据。
                bars = bar_loader(contract, interval, limit=bar_limit, venue=venue)
                # 调用下一根K线信号计算，信号时间等于最新真实K线的收盘时间。
                decision = signal_evaluator(bars, interval, parameters=deployment.strategy)
                # 调用幂等保存；同一K线同一动作重复轮询时created会返回false。
                saved_signals = signal_saver(
                    contract,
                    interval,
                    decision,
                    **({} if venue == GATE_TESTNET_VENUE else {"venue": venue}),
                )
            except Exception as error:
                # 单个品种周期的数据缺口或脏值只封锁本流，其他健康行情仍继续处理。
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="BLOCKED_DATA_ERROR",
                        action_count=0,
                        new_signal_count=0,
                        order_count=0,
                        reason=f"K线或信号处理失败：{type(error).__name__}: {error}",
                    )
                )
                continue
            new_signal_count = sum(signal.created for signal in saved_signals)
            exit_actions = tuple(
                action for action in decision.actions if action.signal_type == "EXIT"
            )
            exit_order_count = 0
            if exit_actions and exit_executor is not None:
                try:
                    # 先调用全部退出动作；无匹配持仓时执行器返回None并安全跳过。
                    exit_results = [
                        exit_executor(
                            symbol=contract,
                            interval=interval,
                            decision=decision,
                            action=action,
                        )
                        for action in exit_actions
                    ]
                    exit_order_count = sum(
                        result is not None and result.created for result in exit_results
                    )
                except Exception as error:
                    stream_results.append(
                        StreamSignalResult(
                            symbol=contract,
                            interval=interval,
                            status="BLOCKED_EXIT_ERROR",
                            action_count=len(decision.actions),
                            new_signal_count=new_signal_count,
                            order_count=0,
                            reason=f"模拟平仓失败：{type(error).__name__}: {error}",
                        )
                    )
                    continue
            if not decision.actions:
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="NO_SIGNAL",
                        action_count=0,
                        new_signal_count=0,
                        order_count=0,
                        reason="最新已收盘K线没有产生EMA进出场动作",
                    )
                )
                continue

            entry_actions = tuple(
                action for action in decision.actions if action.signal_type == "ENTRY"
            )
            if not entry_actions:
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="EXIT_SIGNAL_RECORDED",
                        action_count=len(decision.actions),
                        new_signal_count=new_signal_count,
                        order_count=exit_order_count,
                        reason="退出信号已审计，并优先处理匹配的模拟持仓",
                    )
                )
                continue

            try:
                require_approved_entry_qualification(qualification)
            except PermissionError as error:
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="BLOCKED_QUALIFICATION",
                        action_count=len(decision.actions),
                        new_signal_count=new_signal_count,
                        order_count=exit_order_count,
                        reason=f"{error}；{qualification.reason}",
                    )
                )
                continue

            if entry_executor is None:
                # 未显式提供订单执行器时保持信号观察模式，不创建任何本地订单。
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="READY_FOR_POSITION_SIZING",
                        action_count=len(decision.actions),
                        new_signal_count=new_signal_count,
                        order_count=exit_order_count,
                        reason="准入已通过；当前命令未启用本地订单预留",
                    )
                )
                continue
            try:
                # 每个ENTRY动作调用完整账户、ATR、张数和本地订单门禁；当前策略最多一个方向。
                executions = [
                    entry_executor(
                        symbol=contract,
                        interval=interval,
                        bars=bars,
                        decision=decision,
                        action=action,
                        qualification=qualification,
                        deployment=deployment,
                    )
                    for action in entry_actions
                ]
            except Exception as error:
                stream_results.append(
                    StreamSignalResult(
                        symbol=contract,
                        interval=interval,
                        status="BLOCKED_ORDER_PLAN",
                        action_count=len(decision.actions),
                        new_signal_count=new_signal_count,
                        order_count=exit_order_count,
                        reason=f"本地订单构造或门禁失败：{type(error).__name__}: {error}",
                    )
                )
                continue
            created_orders = sum(execution.order.created for execution in executions)
            created_fills = sum(execution.fill.created for execution in executions)
            total_created_orders = created_orders + exit_order_count
            stream_results.append(
                StreamSignalResult(
                    symbol=contract,
                    interval=interval,
                    status=("PAPER_ENTRY_FILLED" if created_fills else "ENTRY_ALREADY_FILLED"),
                    action_count=len(decision.actions),
                    new_signal_count=new_signal_count,
                    order_count=total_created_orders,
                    reason="本地订单已通过门禁并完成模拟成交；没有发送到交易接口",
                )
            )
    return PaperSignalCycleSummary(streams=tuple(stream_results))
