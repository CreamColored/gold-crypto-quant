"""布林带震荡策略的因果事件回测与三折样本外评估。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from gold_crypto_quant.strategy.bollinger_range import (
    BollingerRangeParameters,
    build_bollinger_entry_context,
    build_same_timeframe_entry_context,
)
from gold_crypto_quant.risk.qualification import QualificationDecision


@dataclass(frozen=True, slots=True)
class BollingerBacktestConfig:
    """回测资金、成本和硬风控设置。"""

    initial_equity: float = 10_000.0
    leverage_limit: float = 125.0
    risk_per_trade: float = 0.0025
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0002
    daily_loss_limit: float = 0.02
    maximum_drawdown: float = 0.08


@dataclass(frozen=True, slots=True)
class BollingerBacktestResult:
    """一段布林带策略回测的净值和交易质量摘要。"""

    symbol: str
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    bar_count: int
    trade_count: int
    total_return: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    average_net_pnl: float
    stop_count: int
    middle_reduction_count: int
    opposite_band_exit_count: int
    daily_fuse_count: int
    drawdown_fuse_triggered: bool

    def to_dict(self) -> dict[str, object]:
        """转换成可以写入JSON审计内容的字典。"""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BollingerQualificationResult:
    """固定参数三折样本外结果。"""

    symbol: str
    interval: str
    parameters: BollingerRangeParameters
    folds: tuple[BollingerBacktestResult, ...]
    compounded_return: float
    worst_drawdown: float
    positive_folds: int
    minimum_fold_trades: int
    approved: bool
    reason: str


def _adverse_price(price: float, side: str, slippage: float) -> float:
    """按市价单的不利方向加入滑点。"""
    return price * (1.0 + slippage if side == "BUY" else 1.0 - slippage)


def run_bollinger_backtest(
    bars_5m: pd.DataFrame,
    bars_15m: pd.DataFrame,
    *,
    symbol: str = "ETH_USDT",
    parameters: BollingerRangeParameters | None = None,
    config: BollingerBacktestConfig | None = None,
    trading_start: pd.Timestamp | None = None,
    same_timeframe: bool = False,
) -> BollingerBacktestResult:
    """逐根处理止损、分批止盈和入场，避免向量化时偷看同根K线的未来顺序。"""
    parameters = parameters or BollingerRangeParameters()
    config = config or BollingerBacktestConfig()
    if len(bars_5m) < 100 or len(bars_15m) < 100:
        raise ValueError("Bollinger backtest requires at least 100 bars per timeframe")
    context = (
        build_same_timeframe_entry_context(bars_5m, parameters)
        if same_timeframe
        else build_bollinger_entry_context(bars_5m, bars_15m, parameters)
    )
    equity = config.initial_equity
    peak_equity = equity
    maximum_drawdown = 0.0
    current_day: pd.Timestamp | None = None
    day_start_equity = equity
    day_blocked = False
    permanent_blocked = False
    daily_fuse_days: set[pd.Timestamp] = set()
    side: str | None = None
    entry_price = 0.0
    stop_price = 0.0
    quantity = 0.0
    remaining_quantity = 0.0
    entry_fee_remaining = 0.0
    middle_reduced = False
    trade_net_pnl = 0.0
    trade_gross_pnl = 0.0
    trade_entry_time: pd.Timestamp | None = None
    net_results: list[float] = []
    stop_count = 0
    middle_reduction_count = 0
    opposite_exit_count = 0
    cooldown_until = {"LONG": -1, "SHORT": -1}
    stopped_regime_counts: dict[int, int] = {}

    def close_quantity(exit_quantity: float, reference: float, exit_side: str) -> float:
        """按当前方向平掉指定数量，并立即把手续费和毛盈亏计入现金权益。"""
        nonlocal equity, remaining_quantity, entry_fee_remaining, trade_net_pnl, trade_gross_pnl
        fill = _adverse_price(reference, exit_side, config.slippage_rate)
        gross = (
            (fill - entry_price) * exit_quantity
            if side == "LONG"
            else (entry_price - fill) * exit_quantity
        )
        exit_fee = abs(fill * exit_quantity * config.fee_rate)
        allocated_entry_fee = entry_fee_remaining * (exit_quantity / remaining_quantity)
        net = gross - exit_fee - allocated_entry_fee
        equity += gross - exit_fee
        trade_gross_pnl += gross
        trade_net_pnl += net
        entry_fee_remaining -= allocated_entry_fee
        remaining_quantity -= exit_quantity
        return fill

    # 第一根只用于提供前一根确认信号；实际成交统一发生在下一根5分钟K线开盘。
    for position in range(1, len(bars_5m)):
        timestamp = pd.Timestamp(bars_5m.index[position])
        bar = bars_5m.iloc[position]
        signal = context.iloc[position - 1]
        utc_day = timestamp.normalize()
        if current_day is None or utc_day != current_day:
            current_day = utc_day
            day_start_equity = equity
            day_blocked = False

        if side is not None:
            # 同根K线同时触发止损和止盈时先按止损处理，这是更保守的成交顺序。
            stop_hit = (
                side == "LONG" and float(bar["low"]) <= stop_price
            ) or (
                side == "SHORT" and float(bar["high"]) >= stop_price
            )
            exit_reason: str | None = None
            if stop_hit:
                reference = min(float(bar["open"]), stop_price) if side == "LONG" else max(float(bar["open"]), stop_price)
                close_quantity(
                    remaining_quantity,
                    reference,
                    "SELL" if side == "LONG" else "BUY",
                )
                stop_count += 1
                exit_reason = "STOP"
                regime_value = signal["regime_id"]
                if pd.notna(regime_value):
                    regime_id = int(regime_value)
                    stopped_regime_counts[regime_id] = stopped_regime_counts.get(regime_id, 0) + 1
                cooldown_until[side] = position + parameters.cooldown_bars_after_stop
            else:
                middle = float(signal["bb_middle"])
                opposite = float(signal["bb_upper"] if side == "LONG" else signal["bb_lower"])
                middle_hit = (
                    side == "LONG" and float(bar["high"]) >= middle
                ) or (
                    side == "SHORT" and float(bar["low"]) <= middle
                )
                opposite_hit = (
                    side == "LONG" and float(bar["high"]) >= opposite
                ) or (
                    side == "SHORT" and float(bar["low"]) <= opposite
                )
                if middle_hit and not middle_reduced:
                    cost_buffer = entry_price * (
                        2 * config.fee_rate + 2 * config.slippage_rate
                    )
                    breakeven_stop = (
                        entry_price + cost_buffer
                        if side == "LONG"
                        else entry_price - cost_buffer
                    )
                    can_place_true_breakeven = (
                        side == "LONG" and breakeven_stop < middle
                    ) or (
                        side == "SHORT" and breakeven_stop > middle
                    )
                    # 中轨空间不足以覆盖成本时调用全平，禁止把“保本损”放到尚未到达的价格。
                    reduce_quantity = (
                        remaining_quantity * parameters.middle_exit_fraction
                        if can_place_true_breakeven
                        else remaining_quantity
                    )
                    close_quantity(
                        reduce_quantity,
                        middle,
                        "SELL" if side == "LONG" else "BUY",
                    )
                    middle_reduced = True
                    middle_reduction_count += 1
                    if remaining_quantity > 1e-12:
                        # 只有价格已经越过真实保本位时，剩余半仓才允许移动保护止损。
                        stop_price = breakeven_stop
                if opposite_hit and remaining_quantity > 1e-12:
                    close_quantity(
                        remaining_quantity,
                        opposite,
                        "SELL" if side == "LONG" else "BUY",
                    )
                    opposite_exit_count += 1
                    exit_reason = "OPPOSITE_BAND"
            if remaining_quantity <= 1e-12:
                # 一次完整入场的所有分批成交合并成一笔交易统计胜负。
                net_results.append(trade_net_pnl)
                side = None
                entry_price = stop_price = quantity = remaining_quantity = 0.0
                entry_fee_remaining = trade_net_pnl = trade_gross_pnl = 0.0
                middle_reduced = False
                trade_entry_time = None

        # 权益风控只禁止新开仓，不篡改已经存在的保护止损和止盈路径。
        if equity / day_start_equity - 1.0 <= -config.daily_loss_limit:
            day_blocked = True
            daily_fuse_days.add(utc_day)
        peak_equity = max(peak_equity, equity)
        drawdown = 1.0 - equity / peak_equity
        maximum_drawdown = max(maximum_drawdown, drawdown)
        if drawdown >= config.maximum_drawdown:
            permanent_blocked = True

        if (
            side is None
            and not day_blocked
            and not permanent_blocked
            and (trading_start is None or timestamp >= trading_start)
        ):
            candidates = []
            if bool(signal["long_entry"]):
                candidates.append("LONG")
            if bool(signal["short_entry"]):
                candidates.append("SHORT")
            # 理论上同一根不应同时触发两端；若异常同时满足则失败关闭而不是猜方向。
            if len(candidates) == 1:
                candidate = candidates[0]
                regime_value = signal["regime_id"]
                regime_stops = (
                    stopped_regime_counts.get(int(regime_value), 0)
                    if pd.notna(regime_value)
                    else parameters.maximum_stops_per_regime
                )
                if position > cooldown_until[candidate] and regime_stops < parameters.maximum_stops_per_regime:
                    order_side = "BUY" if candidate == "LONG" else "SELL"
                    fill = _adverse_price(float(bar["open"]), order_side, config.slippage_rate)
                    stop = fill - parameters.fixed_stop_distance if candidate == "LONG" else fill + parameters.fixed_stop_distance
                    risk_budget = equity * config.risk_per_trade
                    raw_quantity = risk_budget / abs(fill - stop)
                    leverage_cap = equity * config.leverage_limit / fill
                    quantity = min(raw_quantity, leverage_cap)
                    if quantity > 0:
                        fee = fill * quantity * config.fee_rate
                        equity -= fee
                        side = candidate
                        entry_price = fill
                        stop_price = stop
                        remaining_quantity = quantity
                        entry_fee_remaining = fee
                        # 每次分批退出会按数量分摊入场手续费，不能在此提前重复计入。
                        trade_net_pnl = 0.0
                        trade_gross_pnl = 0.0
                        trade_entry_time = timestamp

    if side is not None and remaining_quantity > 0:
        # 数据结束时按最后收盘价强制平仓，防止报告遗漏尚未实现盈亏。
        close_quantity(
            remaining_quantity,
            float(bars_5m.iloc[-1]["close"]),
            "SELL" if side == "LONG" else "BUY",
        )
        net_results.append(trade_net_pnl)

    profits = sum(value for value in net_results if value > 0)
    losses = abs(sum(value for value in net_results if value < 0))
    return BollingerBacktestResult(
        symbol=symbol,
        start_time=pd.Timestamp(trading_start or bars_5m.index[0]),
        end_time=pd.Timestamp(bars_5m.index[-1]),
        bar_count=len(bars_5m),
        trade_count=len(net_results),
        total_return=equity / config.initial_equity - 1.0,
        max_drawdown=maximum_drawdown,
        win_rate=(sum(value > 0 for value in net_results) / len(net_results) if net_results else 0.0),
        profit_factor=(profits / losses if losses > 0 else (float("inf") if profits > 0 else 0.0)),
        average_net_pnl=(float(np.mean(net_results)) if net_results else 0.0),
        stop_count=stop_count,
        middle_reduction_count=middle_reduction_count,
        opposite_band_exit_count=opposite_exit_count,
        daily_fuse_count=len(daily_fuse_days),
        drawdown_fuse_triggered=permanent_blocked,
    )


def qualify_fixed_bollinger_strategy(
    bars_5m: pd.DataFrame,
    bars_15m: pd.DataFrame,
    *,
    symbol: str = "ETH_USDT",
    parameters: BollingerRangeParameters | None = None,
    config: BollingerBacktestConfig | None = None,
    interval: str = "5m",
    same_timeframe: bool = False,
) -> BollingerQualificationResult:
    """把共同交集时间顺序切成三折，固定参数后执行真正样本外门禁。"""
    parameters = parameters or BollingerRangeParameters()
    config = config or BollingerBacktestConfig()
    common_start = max(bars_5m.index.min(), bars_15m.index.min())
    common_end = min(bars_5m.index.max(), bars_15m.index.max())
    usable_5m = bars_5m.loc[(bars_5m.index >= common_start) & (bars_5m.index <= common_end)]
    boundaries = np.linspace(0, len(usable_5m), 4, dtype=int)
    folds: list[BollingerBacktestResult] = []
    for fold in range(3):
        start = max(0, boundaries[fold] - 300)
        stop = boundaries[fold + 1]
        fold_5m_with_warmup = usable_5m.iloc[start:stop]
        test_start = usable_5m.index[boundaries[fold]]
        fold_15m = (
            fold_5m_with_warmup
            if same_timeframe
            else bars_15m.loc[
                (bars_15m.index >= fold_5m_with_warmup.index[0] - pd.Timedelta(days=3))
                & (bars_15m.index <= fold_5m_with_warmup.index[-1])
            ]
        )
        result = run_bollinger_backtest(
            fold_5m_with_warmup,
            fold_15m,
            symbol=symbol,
            parameters=parameters,
            config=config,
            trading_start=pd.Timestamp(test_start),
            same_timeframe=same_timeframe,
        )
        folds.append(result)
    compounded_return = float(np.prod([1.0 + fold.total_return for fold in folds]) - 1.0)
    worst_drawdown = max(fold.max_drawdown for fold in folds)
    positive_folds = sum(fold.total_return > 0 for fold in folds)
    minimum_fold_trades = min(fold.trade_count for fold in folds)
    failures: list[str] = []
    if compounded_return <= 0:
        failures.append(f"三折复合收益{compounded_return:.2%}未高于0%")
    if worst_drawdown >= config.maximum_drawdown:
        failures.append(f"最差回撤{worst_drawdown:.2%}达到或超过8%")
    if positive_folds < 2:
        failures.append(f"盈利窗口{positive_folds}/3少于2个")
    if minimum_fold_trades < 8:
        failures.append(f"单折最少交易{minimum_fold_trades}笔少于8笔")
    if any(fold.profit_factor < 1.2 for fold in folds):
        failures.append("至少一个样本外窗口Profit Factor低于1.20")
    return BollingerQualificationResult(
        symbol=symbol,
        interval=interval,
        parameters=parameters,
        folds=tuple(folds),
        compounded_return=compounded_return,
        worst_drawdown=worst_drawdown,
        positive_folds=positive_folds,
        minimum_fold_trades=minimum_fold_trades,
        approved=not failures,
        reason="通过全部布林带样本外准入门槛" if not failures else "；".join(failures),
    )


def build_bollinger_qualification_decision(
    result: BollingerQualificationResult,
    *,
    evaluated_at: datetime | None = None,
) -> QualificationDecision:
    """把固定参数三折结果转换成现有数据库可持久化的统一准入决定。"""
    evaluated_at = evaluated_at or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    criteria: dict[str, object] = {
        "minimum_compounded_return": 0.0,
        "maximum_worst_drawdown": 0.08,
        "minimum_positive_folds": 2,
        "minimum_trades_per_fold": 8,
        "minimum_profit_factor_per_fold": 1.2,
    }
    fold_details: list[dict[str, object]] = []
    for fold in result.folds:
        values = fold.to_dict()
        values["start_time"] = fold.start_time.isoformat()
        values["end_time"] = fold.end_time.isoformat()
        if not np.isfinite(fold.profit_factor):
            values["profit_factor"] = None
        fold_details.append(values)
    details: dict[str, object] = {
        "stable_parameter_set": True,
        "parameters": result.parameters.to_dict(),
        "folds": fold_details,
        "data_warning": "Gate测试网5分钟历史受10000点限制，本次仅使用最近连续区间",
    }
    payload = {
        "strategy_name": "BOLLINGER_RANGE",
        "strategy_version": "3.0.0",
        "symbol": result.symbol,
        "interval": result.interval,
        "criteria": criteria,
        "details": details,
    }
    # 调用稳定JSON序列化和SHA-256，完全相同的数据与参数重复执行不会生成重复记录。
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    evaluation_hash = hashlib.sha256(encoded).hexdigest()
    return QualificationDecision(
        evaluation_hash=evaluation_hash,
        strategy_name="BOLLINGER_RANGE",
        strategy_version="3.0.0",
        symbol=result.symbol,
        interval=result.interval,
        approved=result.approved,
        compounded_return=result.compounded_return,
        worst_drawdown=result.worst_drawdown,
        positive_folds=result.positive_folds,
        total_folds=len(result.folds),
        minimum_fold_trades=result.minimum_fold_trades,
        criteria=criteria,
        details=details,
        reason=result.reason,
        evaluated_at=evaluated_at,
    )
