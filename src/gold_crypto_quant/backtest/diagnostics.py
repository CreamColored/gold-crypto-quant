"""EMA回测逐笔交易诊断和亏损归因。"""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
import vectorbt as vbt

from gold_crypto_quant.backtest.ema_vectorbt import PANDAS_FREQUENCIES, EmaBacktestConfig
from gold_crypto_quant.risk.indicators import average_true_range
from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters, generate_ema_signals


@dataclass(frozen=True, slots=True)
class TradeDiagnostics:
    """单品种周期的逐笔交易归因摘要。"""

    symbol: str
    interval: str
    trade_count: int
    net_pnl: float
    pnl_before_fees: float
    total_fees: float
    estimated_slippage: float
    long_count: int
    long_pnl: float
    short_count: int
    short_pnl: float
    atr_stop_count: int
    open_price_exit_count: int
    other_exit_count: int
    average_holding_bars: float
    longest_losing_streak: int
    winning_trend_strength: float
    losing_trend_strength: float
    average_entry_atr_percent: float

    def to_dict(self) -> dict[str, object]:
        """转换为便于后续写入JSON或数据库的普通字典。"""
        return asdict(self)


def _mean_or_zero(values: pd.Series) -> float:
    """空序列或NaN返回0，避免诊断报告出现不可序列化数值。"""
    if values.empty:
        return 0.0
    result = float(values.mean())
    return result if np.isfinite(result) else 0.0


def _longest_losing_streak(pnl: pd.Series) -> int:
    """按交易结束顺序计算最长连续亏损笔数。"""
    longest = 0
    current = 0
    for value in pnl:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def diagnose_trades(
    bars: pd.DataFrame,
    portfolio: vbt.Portfolio,
    *,
    symbol: str,
    interval: str,
    strategy: EmaTrendParameters | None = None,
    config: EmaBacktestConfig | None = None,
) -> TradeDiagnostics:
    """分析方向、成本、退出类型、持仓时间、连亏和进场趋势强度。"""
    strategy = strategy or EmaTrendParameters()
    config = config or EmaBacktestConfig()
    if interval not in PANDAS_FREQUENCIES:
        raise ValueError(f"unsupported diagnostic interval: {interval}")

    # 调用vectorbt可读交易记录，保留每笔交易的方向、费用、时间与净盈亏。
    trades = portfolio.trades.records_readable.copy()
    # 调用vectorbt可读订单记录，用订单名义金额估算滑点造成的成本。
    orders = portfolio.orders.records_readable.copy()
    if trades.empty:
        return TradeDiagnostics(
            symbol=symbol,
            interval=interval,
            trade_count=0,
            net_pnl=0.0,
            pnl_before_fees=0.0,
            total_fees=0.0,
            estimated_slippage=0.0,
            long_count=0,
            long_pnl=0.0,
            short_count=0,
            short_pnl=0.0,
            atr_stop_count=0,
            open_price_exit_count=0,
            other_exit_count=0,
            average_holding_bars=0.0,
            longest_losing_streak=0,
            winning_trend_strength=0.0,
            losing_trend_strength=0.0,
            average_entry_atr_percent=0.0,
        )

    total_fees = float(trades["Entry Fees"].sum() + trades["Exit Fees"].sum())
    net_pnl = float(trades["PnL"].sum())
    order_notional = orders["Size"].astype(float) * orders["Price"].astype(float)
    # 滑点无法从成交记录反推出无滑点反事实价格，因此按成交名义金额乘配置比例估算。
    estimated_slippage = float(order_notional.sum() * config.slippage_rate)

    long_trades = trades[trades["Direction"] == "Long"]
    short_trades = trades[trades["Direction"] == "Short"]
    interval_delta = pd.Timedelta(PANDAS_FREQUENCIES[interval])
    holding_time = trades["Exit Timestamp"] - trades["Entry Timestamp"]
    average_holding_bars = float((holding_time / interval_delta).mean())

    # 调用策略方法重新生成进场时可见的EMA，用EMA差/ATR衡量趋势强度。
    indicators = generate_ema_signals(bars, strategy)
    # 调用ATR方法，并移动一根K线，确保诊断使用的波动率与下单时可见信息一致。
    atr = average_true_range(bars, config.atr_period).shift(1)
    # 进场发生在下一根开盘，所以EMA同样移动一根，禁止用执行K线收盘后的指标做归因。
    trend_strength = (indicators["ema_fast"] - indicators["ema_slow"]).abs().shift(1) / atr
    entry_atr_percent = atr / bars["close"]

    trade_strength = trades["Entry Timestamp"].map(trend_strength)
    trade_atr_percent = trades["Entry Timestamp"].map(entry_atr_percent)
    winners = trades["PnL"] > 0

    atr_stop_count = 0
    open_price_exit_count = 0
    other_exit_count = 0
    stop_fraction = atr * config.atr_multiple / bars["open"]
    for _trade_index, trade in trades.iterrows():
        entry_time = trade["Entry Timestamp"]
        entry_price_reference = float(bars.at[entry_time, "open"])
        exit_time = trade["Exit Timestamp"]
        exit_price = float(trade["Avg Exit Price"])
        direction = trade["Direction"]
        fraction = float(stop_fraction.at[entry_time])
        if direction == "Long":
            expected_stop = entry_price_reference * (1.0 - fraction) * (1.0 - config.slippage_rate)
            expected_open_exit = float(bars.at[exit_time, "open"]) * (1.0 - config.slippage_rate)
        else:
            expected_stop = entry_price_reference * (1.0 + fraction) * (1.0 + config.slippage_rate)
            expected_open_exit = float(bars.at[exit_time, "open"]) * (1.0 + config.slippage_rate)

        if np.isclose(exit_price, expected_stop, rtol=1e-6):
            atr_stop_count += 1
        elif np.isclose(exit_price, expected_open_exit, rtol=1e-6):
            open_price_exit_count += 1
        else:
            # 跳空止损或最后一根K线的未平仓盯市价格会进入其他类别。
            other_exit_count += 1

    return TradeDiagnostics(
        symbol=symbol,
        interval=interval,
        trade_count=len(trades),
        net_pnl=net_pnl,
        pnl_before_fees=net_pnl + total_fees,
        total_fees=total_fees,
        estimated_slippage=estimated_slippage,
        long_count=len(long_trades),
        long_pnl=float(long_trades["PnL"].sum()),
        short_count=len(short_trades),
        short_pnl=float(short_trades["PnL"].sum()),
        atr_stop_count=atr_stop_count,
        open_price_exit_count=open_price_exit_count,
        other_exit_count=other_exit_count,
        average_holding_bars=average_holding_bars,
        longest_losing_streak=_longest_losing_streak(trades["PnL"]),
        winning_trend_strength=_mean_or_zero(trade_strength[winners]),
        losing_trend_strength=_mean_or_zero(trade_strength[~winners]),
        average_entry_atr_percent=_mean_or_zero(trade_atr_percent),
    )
