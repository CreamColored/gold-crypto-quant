"""把最新已收盘K线转换为下一根K线可执行的EMA信号。"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pandas as pd

from gold_crypto_quant.risk.indicators import average_directional_index, average_true_range
from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters, generate_ema_signals
from gold_crypto_quant.strategy.higher_timeframe import build_higher_timeframe_filter

INTERVAL_DURATION = {
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
}


@dataclass(frozen=True, slots=True)
class SignalAction:
    """一条需要审计保存的策略动作。"""

    side: str
    signal_type: str


@dataclass(frozen=True, slots=True)
class NextBarSignalDecision:
    """最新收盘确认后、计划在下一根开盘处理的策略决定。"""

    bar_time: datetime
    signal_price: Decimal
    actions: tuple[SignalAction, ...]
    payload: dict[str, object]


def build_signal_dedupe_key(
    *,
    strategy_name: str,
    strategy_version: str,
    instrument_id: int,
    interval: str,
    bar_time: datetime,
    action: SignalAction,
) -> str:
    """生成不依赖进程或重跑次数的确定性信号去重键。"""
    if bar_time.tzinfo is None:
        raise ValueError("signal bar_time must be timezone-aware")
    identity = "|".join(
        (
            strategy_name,
            strategy_version,
            str(instrument_id),
            interval,
            bar_time.isoformat(),
            action.side,
            action.signal_type,
        )
    )
    # 调用SHA-256生成固定64位键，与strategy_signals唯一字段长度一致。
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def evaluate_next_bar_signal(
    bars: pd.DataFrame,
    interval: str,
    *,
    parameters: EmaTrendParameters | None = None,
) -> NextBarSignalDecision:
    """用最新真实收盘K线判断下一根开盘时刻的EMA动作。"""
    parameters = parameters or EmaTrendParameters()
    if interval not in INTERVAL_DURATION:
        raise ValueError(f"unsupported signal interval: {interval}")
    if bars.empty:
        raise ValueError("bars cannot be empty")
    if bars.index.tz is None:
        raise ValueError("bars index must be timezone-aware")

    latest_close = Decimal(str(bars.iloc[-1]["close"]))
    next_bar_time = bars.index[-1] + INTERVAL_DURATION[interval]
    synthetic = bars.iloc[[-1]].copy()
    synthetic.index = pd.DatetimeIndex([next_bar_time])
    # 合成行只用于承接策略内部shift(1)后的信号；所有价格固定为已知最新收盘价。
    for price_column in ("open", "high", "low", "close"):
        synthetic.loc[next_bar_time, price_column] = float(latest_close)
    for volume_column in ("volume", "quote_volume"):
        if volume_column in synthetic.columns:
            synthetic.loc[next_bar_time, volume_column] = 0.0
    combined = pd.concat([bars, synthetic])
    # 调用正式EMA策略；合成行的动作只来自最后一根真实K线向后移动的确认信号。
    calculated = generate_ema_signals(combined, parameters)
    execution_row = calculated.iloc[-1].copy()
    source_row = calculated.iloc[-2]

    # 与正式回测一致，执行时只使用最后一根真实收盘K线的ATR和EMA差。
    source_atr = average_true_range(combined, 14).iloc[-2]
    trend_strength = (
        abs(float(source_row["ema_fast"]) - float(source_row["ema_slow"]))
        / float(source_atr)
        if pd.notna(source_atr) and float(source_atr) > 0
        else None
    )
    if trend_strength is None or trend_strength < parameters.min_trend_strength:
        execution_row["long_entry"] = False
        execution_row["short_entry"] = False
    source_adx: float | None = None
    if parameters.min_adx > 0:
        # 调用Wilder ADX并读取真实收盘行，与回测的shift(1)执行语义相同。
        adx_value = average_directional_index(combined, 14).iloc[-2]
        source_adx = float(adx_value) if pd.notna(adx_value) else None
        if source_adx is None or source_adx < parameters.min_adx:
            execution_row["long_entry"] = False
            execution_row["short_entry"] = False
    higher_long_allowed: bool | None = None
    higher_short_allowed: bool | None = None
    if parameters.use_higher_timeframe_filter:
        # 合成行位于下一根开盘，调用高周期过滤时只会传播已经完整结束的高周期结果。
        higher_filter = build_higher_timeframe_filter(
            combined,
            interval,
            ema_period=parameters.trend_period,
        )
        higher_long_allowed = bool(higher_filter.iloc[-1]["higher_long_allowed"])
        higher_short_allowed = bool(higher_filter.iloc[-1]["higher_short_allowed"])
        execution_row["long_entry"] = bool(execution_row["long_entry"]) and higher_long_allowed
        execution_row["short_entry"] = (
            bool(execution_row["short_entry"]) and higher_short_allowed
        )
    if not parameters.allow_long:
        execution_row["long_entry"] = False
    if not parameters.allow_short:
        execution_row["short_entry"] = False

    action_columns = (
        ("long_entry", SignalAction("LONG", "ENTRY")),
        ("short_entry", SignalAction("SHORT", "ENTRY")),
        ("long_exit", SignalAction("LONG", "EXIT")),
        ("short_exit", SignalAction("SHORT", "EXIT")),
    )
    actions = tuple(action for column, action in action_columns if bool(execution_row[column]))

    def optional_float(value: object) -> float | None:
        """把有效指标转成JSON可保存浮点数，预热期NaN改为None。"""
        return float(value) if pd.notna(value) else None

    return NextBarSignalDecision(
        bar_time=next_bar_time.to_pydatetime(),
        signal_price=latest_close,
        actions=actions,
        payload={
            "source_bar_open_time": bars.index[-1].isoformat(),
            "execution_rule": "NEXT_BAR_OPEN_AFTER_CONFIRMED_CLOSE",
            "ema_fast": optional_float(source_row["ema_fast"]),
            "ema_slow": optional_float(source_row["ema_slow"]),
            "ema_trend": optional_float(source_row["ema_trend"]),
            "trend_strength": trend_strength,
            "adx": source_adx,
            "higher_long_allowed": higher_long_allowed,
            "higher_short_allowed": higher_short_allowed,
            "entry_mode": parameters.entry_mode,
            # 保存斜率观察窗口，便于模拟交易信号与批准参数逐笔审计核对。
            "trend_slope_lookback": parameters.trend_slope_lookback,
        },
    )
