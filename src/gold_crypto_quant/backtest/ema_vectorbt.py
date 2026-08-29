"""基于vectorbt的EMA 20/50/200多空回测。"""

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
import vectorbt as vbt

from gold_crypto_quant.risk.indicators import average_directional_index, average_true_range
from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters, generate_ema_signals
from gold_crypto_quant.strategy.higher_timeframe import (
    build_ema_alignment_filter,
    build_higher_timeframe_filter,
)

PANDAS_FREQUENCIES = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
}


@dataclass(frozen=True, slots=True)
class EmaBacktestConfig:
    """第一版回测资金、成本和止损参数。"""

    initial_equity: float = 10_000.0
    leverage_limit: float = 125.0
    risk_per_trade: float = 0.0025
    atr_period: int = 14
    atr_multiple: float = 1.5
    take_profit_atr_multiple: float = 0.0
    trailing_stop: bool = False
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0002
    daily_loss_limit: float = 0.02
    max_drawdown_limit: float = 0.08

    def __post_init__(self) -> None:
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if not 1 <= self.leverage_limit <= 125:
            raise ValueError("leverage_limit must be in [1, 125]")
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("risk_per_trade must be in (0, 0.02]")
        if self.atr_period < 2 or self.atr_multiple <= 0:
            raise ValueError("ATR settings must be positive")
        if not 0 <= self.take_profit_atr_multiple <= 20:
            raise ValueError("take_profit_atr_multiple must be in [0, 20]")
        if self.fee_rate < 0 or self.slippage_rate < 0:
            raise ValueError("trading costs cannot be negative")
        if not 0 < self.daily_loss_limit <= 0.1:
            raise ValueError("daily_loss_limit must be in (0, 0.1]")
        if not self.daily_loss_limit < self.max_drawdown_limit <= 0.5:
            raise ValueError("max_drawdown_limit must exceed the daily loss limit")


@dataclass(frozen=True, slots=True)
class EmaBacktestResult:
    """使用真实账户权益口径计算的回测摘要。"""

    symbol: str
    interval: str
    bar_count: int
    trade_count: int
    total_pnl: float
    total_return: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    start_time: pd.Timestamp
    end_time: pd.Timestamp
    passed_drawdown_limit: bool
    daily_fuse_count: int
    drawdown_fuse_triggered: bool
    drawdown_fuse_time: pd.Timestamp | None

    def to_dict(self) -> dict[str, object]:
        """转换成便于打印或后续写入JSON报告的字典。"""
        return asdict(self)


def _safe_metric(value: object, default: float = 0.0) -> float:
    """把vectorbt可能返回的NumPy标量或NaN转换为普通float。"""
    result = float(value)
    return default if not math.isfinite(result) else result


def _build_portfolio(
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    order_size: pd.Series,
    stop_fraction: pd.Series,
    entry_allowed: pd.Series,
    risk_exit: pd.Series,
    config: EmaBacktestConfig,
    interval: str,
) -> vbt.Portfolio:
    """根据当前风控遮罩构建一次vectorbt组合。"""
    # 风控遮罩只阻止新开仓；策略退出或熔断退出始终允许执行。
    long_entries = signals["long_entry"] & entry_allowed
    short_entries = signals["short_entry"] & entry_allowed
    long_exits = signals["long_exit"] | risk_exit
    short_exits = signals["short_exit"] | risk_exit
    simulation_buying_power = config.initial_equity * config.leverage_limit
    if config.take_profit_atr_multiple > 0:
        # 止盈与止损使用同一根已收盘K线的ATR；按倍数比例换算可避免重复计算波动率。
        take_profit_fraction: pd.Series | float = (
            stop_fraction * config.take_profit_atr_multiple / config.atr_multiple
        )
    else:
        # vectorbt使用NaN表示不启用固定止盈；默认行为与原回测完全一致。
        take_profit_fraction = float("nan")

    # 调用vectorbt执行多空信号、手续费、滑点、ATR止损、可选止盈和移动止损模拟。
    return vbt.Portfolio.from_signals(
        close=bars["close"],
        entries=long_entries,
        exits=long_exits,
        short_entries=short_entries,
        short_exits=short_exits,
        size=order_size,
        size_type="amount",
        price=bars["open"],
        open=bars["open"],
        high=bars["high"],
        low=bars["low"],
        sl_stop=stop_fraction,
        sl_trail=config.trailing_stop,
        tp_stop=take_profit_fraction,
        stop_entry_price="price",
        stop_exit_price="stopmarket",
        upon_opposite_entry="reversereduce",
        accumulate=False,
        fees=config.fee_rate,
        slippage=config.slippage_rate,
        init_cash=simulation_buying_power,
        # 将项目周期代码转换为Pandas频率，避免字母m在不同版本中产生分钟/月歧义。
        freq=PANDAS_FREQUENCIES[interval],
    )


def _real_equity_curve(portfolio: vbt.Portfolio, config: EmaBacktestConfig) -> pd.Series:
    """把vectorbt模拟购买力换算为真实账户权益。"""
    simulation_buying_power = config.initial_equity * config.leverage_limit
    # 调用portfolio.value取得逐根K线盯市权益，再扣除仅用于模拟杠杆的额外购买力。
    simulated_value = portfolio.value()
    return config.initial_equity + (simulated_value - simulation_buying_power)


def _detect_daily_fuses(
    equity_curve: pd.Series,
    config: EmaBacktestConfig,
) -> list[tuple[pd.Timestamp, int]]:
    """按时间顺序返回每日亏损首次达到阈值的位置。"""
    breaches: list[tuple[pd.Timestamp, int]] = []

    # UTC是数据库和交易系统的统一日界线；每日基准取当天第一根K线之前的账户权益。
    utc_days = equity_curve.index.normalize()
    for day in utc_days.unique():
        day_positions = np.flatnonzero(utc_days == day)
        first_position = int(day_positions[0])
        day_start_equity = (
            config.initial_equity
            if first_position == 0
            else float(equity_curve.iloc[first_position - 1])
        )
        day_returns = equity_curve.iloc[day_positions] / day_start_equity - 1.0
        breached = np.flatnonzero(day_returns.to_numpy() <= -config.daily_loss_limit)
        if breached.size == 0:
            continue

        breach_position = int(day_positions[int(breached[0])])
        breaches.append((pd.Timestamp(day), breach_position))
    return breaches


def _detect_drawdown_fuse(
    equity_curve: pd.Series,
    config: EmaBacktestConfig,
) -> int | None:
    """返回总回撤首次达到阈值的位置；未达到时返回None。"""
    # 总回撤基于历史权益峰值，阈值默认是项目规则规定的8%。
    drawdown = equity_curve / equity_curve.cummax() - 1.0
    breached_drawdown = np.flatnonzero(drawdown.to_numpy() <= -config.max_drawdown_limit)
    return int(breached_drawdown[0]) if breached_drawdown.size else None


def _run_risk_controlled_portfolio(
    bars: pd.DataFrame,
    signals: pd.DataFrame,
    order_size: pd.Series,
    stop_fraction: pd.Series,
    initial_entry_allowed: pd.Series,
    config: EmaBacktestConfig,
    interval: str,
) -> tuple[vbt.Portfolio, pd.Series, set[pd.Timestamp], pd.Timestamp | None]:
    """在指定开仓遮罩上应用每日和总回撤熔断。"""
    entry_allowed = initial_entry_allowed.copy()
    risk_exit = pd.Series(False, index=bars.index, dtype=bool)
    daily_fuse_days: set[pd.Timestamp] = set()
    maximum_iterations = len(bars.index.normalize().unique()) + 1
    for _iteration in range(maximum_iterations):
        # 按当前已知的熔断限制调用vectorbt重新构建组合。
        portfolio = _build_portfolio(
            bars,
            signals,
            order_size,
            stop_fraction,
            entry_allowed,
            risk_exit,
            config,
            interval,
        )
        # 调用换算方法得到真实资金口径的权益曲线。
        equity_curve = _real_equity_curve(portfolio, config)
        # 调用每日熔断检测，并且每轮只落实时间最早的新事件，保证后续事件基于新权益重算。
        daily_breaches = _detect_daily_fuses(equity_curve, config)
        new_breach = next(
            (item for item in daily_breaches if item[0] not in daily_fuse_days),
            None,
        )
        if new_breach is None:
            break
        breach_day, breach_position = new_breach
        daily_fuse_days.add(breach_day)
        utc_days = bars.index.normalize()
        # 收盘后才知道触发熔断，因此从下一根K线开始禁止当天的新开仓并执行平仓。
        remaining_day = (utc_days == breach_day) & (np.arange(len(bars)) > breach_position)
        entry_allowed.iloc[np.flatnonzero(remaining_day)] = False
        if breach_position + 1 < len(bars):
            risk_exit.iloc[breach_position + 1] = True
    else:
        raise RuntimeError("daily risk fuse calculation did not converge")

    drawdown_fuse_time: pd.Timestamp | None = None
    # 每日熔断全部按因果顺序稳定后，再检查是否仍然达到策略总回撤阈值。
    drawdown_breach_position = _detect_drawdown_fuse(equity_curve, config)
    if drawdown_breach_position is not None:
        drawdown_fuse_time = pd.Timestamp(equity_curve.index[drawdown_breach_position])
        entry_allowed.iloc[drawdown_breach_position + 1 :] = False
        if drawdown_breach_position + 1 < len(bars):
            risk_exit.iloc[drawdown_breach_position + 1] = True
        # 应用永久熔断遮罩后调用vectorbt完成最终一次组合计算。
        portfolio = _build_portfolio(
            bars,
            signals,
            order_size,
            stop_fraction,
            entry_allowed,
            risk_exit,
            config,
            interval,
        )
        # 调用权益换算方法生成最终报告所使用的资金曲线。
        equity_curve = _real_equity_curve(portfolio, config)
    return portfolio, equity_curve, daily_fuse_days, drawdown_fuse_time


def _find_atr_stop_exit_times(
    bars: pd.DataFrame,
    portfolio: vbt.Portfolio,
    stop_fraction: pd.Series,
    config: EmaBacktestConfig,
) -> list[pd.Timestamp]:
    """按时间返回价格与ATR止损成交价一致的退出时点。"""
    stop_times: list[pd.Timestamp] = []
    # 调用可读交易记录，使用进场参考价和方向重建该笔交易的ATR止损成交价。
    trades = portfolio.trades.records_readable
    for _trade_index, trade in trades.iterrows():
        entry_time = pd.Timestamp(trade["Entry Timestamp"])
        exit_time = pd.Timestamp(trade["Exit Timestamp"])
        reference_entry = float(bars.at[entry_time, "open"])
        fraction = float(stop_fraction.at[entry_time])
        if trade["Direction"] == "Long":
            expected_exit = reference_entry * (1.0 - fraction) * (1.0 - config.slippage_rate)
        else:
            expected_exit = reference_entry * (1.0 + fraction) * (1.0 + config.slippage_rate)
        if np.isclose(float(trade["Avg Exit Price"]), expected_exit, rtol=1e-6):
            stop_times.append(exit_time)
    return sorted(set(stop_times))


def run_ema_backtest(
    bars: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    strategy: EmaTrendParameters | None = None,
    config: EmaBacktestConfig | None = None,
    trade_start: pd.Timestamp | None = None,
) -> tuple[EmaBacktestResult, vbt.Portfolio]:
    """运行单品种、单周期EMA多空回测，并返回摘要和vectorbt组合。"""
    strategy = strategy or EmaTrendParameters()
    config = config or EmaBacktestConfig()
    required_columns = {"open", "high", "low", "close"}
    missing_columns = required_columns - set(bars.columns)
    if missing_columns:
        raise ValueError(f"bars missing required columns: {sorted(missing_columns)}")
    if len(bars) <= strategy.trend_period:
        raise ValueError("not enough bars to warm up the trend EMA")
    if interval not in PANDAS_FREQUENCIES:
        raise ValueError(f"unsupported backtest interval: {interval}")
    if config.trailing_stop and strategy.cooldown_bars > 0:
        raise ValueError("trailing stop cannot be combined with ATR stop cooldown")

    # generate_ema_signals已把收盘确认信号移动到下一根K线，因此price使用该根开盘价。
    signals = generate_ema_signals(bars, strategy)
    # ATR使用上一根已收盘K线的值，禁止读取执行K线尚未完成的最高价和最低价。
    atr = average_true_range(bars, config.atr_period).shift(1)
    stop_distance = atr * config.atr_multiple
    stop_fraction = (stop_distance / bars["open"]).replace([np.inf, -np.inf], np.nan)

    # 执行K线只能使用上一根已收盘K线的EMA差和ATR，shift(1)用于杜绝未来数据。
    entry_trend_strength = (signals["ema_fast"] - signals["ema_slow"]).abs().shift(1) / atr
    signals = signals.copy()
    signals["long_entry"] &= entry_trend_strength.ge(strategy.min_trend_strength)
    signals["short_entry"] &= entry_trend_strength.ge(strategy.min_trend_strength)
    if strategy.min_adx > 0:
        # 调用ADX并移动一根K线，执行开盘只能使用上一根已收盘K线确认的趋势强度。
        entry_adx = average_directional_index(bars, config.atr_period).shift(1)
        signals["long_entry"] &= entry_adx.ge(strategy.min_adx)
        signals["short_entry"] &= entry_adx.ge(strategy.min_adx)
    if strategy.use_higher_timeframe_filter:
        # EMA12回踩调用专用的12/144/169高周期排列确认；原EMA策略继续使用EMA200过滤。
        if strategy.entry_mode == "ema12_pullback":
            higher_filter = build_ema_alignment_filter(
                bars,
                interval,
                fast_period=strategy.fast_period,
                slow_period=strategy.slow_period,
                trend_period=strategy.trend_period,
            )
        else:
            higher_filter = build_higher_timeframe_filter(
                bars,
                interval,
                mode=strategy.higher_timeframe_mode,
            )
        signals["long_entry"] &= higher_filter["higher_long_allowed"]
        signals["short_entry"] &= higher_filter["higher_short_allowed"]
    if not strategy.allow_long:
        signals["long_entry"] = False
    if not strategy.allow_short:
        signals["short_entry"] = False

    # 每笔允许亏损固定初始权益的0.25%；仓位数量由ATR止损距离反推。
    risk_amount = config.initial_equity * config.risk_per_trade
    risk_limited_size = risk_amount / stop_distance
    leverage_limited_size = config.initial_equity * config.leverage_limit / bars["open"]
    order_size = pd.concat([risk_limited_size, leverage_limited_size], axis=1).min(axis=1)
    order_size = order_size.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    # 基础遮罩统一承载验证集起点与止损冷却，不包含权益相关熔断。
    base_entry_allowed = pd.Series(True, index=bars.index, dtype=bool)
    if trade_start is not None:
        normalized_trade_start = pd.Timestamp(trade_start)
        if normalized_trade_start.tzinfo is None:
            raise ValueError("trade_start must be timezone-aware")
        if normalized_trade_start > bars.index[-1]:
            raise ValueError("trade_start must not be after the final bar")
        # 预热K线只用于计算指标，trade_start之前不允许产生任何开仓订单。
        base_entry_allowed &= bars.index >= normalized_trade_start

    cooldown_allowed = pd.Series(True, index=bars.index, dtype=bool)
    confirmed_stop_times: set[pd.Timestamp] = set()
    for _cooldown_iteration in range(len(bars) + 1):
        # 每次调用完整熔断流程，确保冷却期改变交易后权益风控也同步重算。
        portfolio, equity_curve, daily_fuse_days, drawdown_fuse_time = (
            _run_risk_controlled_portfolio(
                bars,
                signals,
                order_size,
                stop_fraction,
                base_entry_allowed & cooldown_allowed,
                config,
                interval,
            )
        )
        if strategy.cooldown_bars == 0:
            break
        # 调用止损识别方法，每轮只确认时间最早的新止损，避免保留反事实的后续交易。
        stop_times = _find_atr_stop_exit_times(bars, portfolio, stop_fraction, config)
        new_stop_time = next(
            (time for time in stop_times if time not in confirmed_stop_times),
            None,
        )
        if new_stop_time is None:
            break
        confirmed_stop_times.add(new_stop_time)
        exit_position = int(bars.index.get_loc(new_stop_time))
        cooldown_end = min(len(bars), exit_position + 1 + strategy.cooldown_bars)
        # 止损所在K线结束后，从下一根开始屏蔽指定数量的开仓机会。
        cooldown_allowed.iloc[exit_position + 1 : cooldown_end] = False
    else:
        raise RuntimeError("ATR stop cooldown calculation did not converge")

    running_peak = equity_curve.cummax()
    drawdown = equity_curve / running_peak - 1.0
    total_pnl = float(equity_curve.iloc[-1] - config.initial_equity)
    max_drawdown = abs(float(drawdown.min()))

    trade_count = int(portfolio.trades.count())
    win_rate = _safe_metric(portfolio.trades.win_rate()) if trade_count else 0.0
    profit_factor = _safe_metric(portfolio.trades.profit_factor()) if trade_count else 0.0
    result = EmaBacktestResult(
        symbol=symbol,
        interval=interval,
        bar_count=int((bars.index >= trade_start).sum()) if trade_start is not None else len(bars),
        trade_count=trade_count,
        total_pnl=total_pnl,
        total_return=total_pnl / config.initial_equity,
        max_drawdown=max_drawdown,
        win_rate=win_rate,
        profit_factor=profit_factor,
        start_time=pd.Timestamp(trade_start) if trade_start is not None else bars.index[0],
        end_time=bars.index[-1],
        passed_drawdown_limit=max_drawdown < config.max_drawdown_limit,
        daily_fuse_count=len(daily_fuse_days),
        drawdown_fuse_triggered=drawdown_fuse_time is not None,
        drawdown_fuse_time=drawdown_fuse_time,
    )
    return result, portfolio
