"""EMA 20/50/200 趋势策略及下一根 K 线执行信号。"""

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True, slots=True)
class EmaTrendParameters:
    """EMA 周期配置；默认值分别代表短期、中期和大趋势。"""

    fast_period: int = 20
    slow_period: int = 50
    trend_period: int = 200
    min_trend_strength: float = 0.0
    allow_long: bool = True
    allow_short: bool = True
    min_adx: float = 0.0
    use_higher_timeframe_filter: bool = False
    higher_timeframe_mode: str = "standard"
    trend_slope_lookback: int = 0
    cooldown_bars: int = 0
    entry_mode: str = "cross"
    pullback_lookback: int = 3
    pullback_slope_lookback: int = 5
    pullback_max_penetration_atr: float = 0.5
    pullback_min_departure_atr: float = 0.15

    def __post_init__(self) -> None:
        """在创建参数对象时检查周期顺序，防止策略含义被错误配置。"""
        if not 1 < self.fast_period < self.slow_period < self.trend_period:
            raise ValueError("EMA periods must satisfy 1 < fast < slow < trend")
        if not 0 <= self.min_trend_strength <= 10:
            raise ValueError("min_trend_strength must be in [0, 10]")
        if not self.allow_long and not self.allow_short:
            raise ValueError("at least one trading direction must be enabled")
        if not 0 <= self.min_adx <= 100:
            raise ValueError("min_adx must be in [0, 100]")
        if not 0 <= self.cooldown_bars <= 100:
            raise ValueError("cooldown_bars must be in [0, 100]")
        if not 0 <= self.trend_slope_lookback <= 100:
            raise ValueError("trend_slope_lookback must be in [0, 100]")
        if self.higher_timeframe_mode not in {"standard", "macro"}:
            raise ValueError("higher_timeframe_mode must be 'standard' or 'macro'")
        if self.entry_mode not in {"cross", "pullback", "ema12_pullback", "breakout"}:
            raise ValueError("unsupported entry_mode")
        if self.entry_mode == "breakout":
            if not 2 <= self.pullback_lookback <= 200:
                raise ValueError("breakout lookback must be in [2, 200]")
        elif not 1 <= self.pullback_lookback <= 20:
            raise ValueError("pullback_lookback must be in [1, 20]")
        if not 1 <= self.pullback_slope_lookback <= 20:
            raise ValueError("pullback_slope_lookback must be in [1, 20]")
        if not 0 <= self.pullback_max_penetration_atr <= 5:
            raise ValueError("pullback_max_penetration_atr must be in [0, 5]")
        if not 0 <= self.pullback_min_departure_atr <= 5:
            raise ValueError("pullback_min_departure_atr must be in [0, 5]")


def generate_ema_signals(
    bars: pd.DataFrame,
    parameters: EmaTrendParameters | None = None,
) -> pd.DataFrame:
    """根据已收盘 K 线生成 EMA 指标和下一根 K 线的多空执行信号。

    Args:
        bars: 时间升序且索引唯一的 K 线，必须包含 ``close`` 列。
        parameters: EMA 周期；不传时使用 20/50/200。

    Returns:
        包含三条 EMA 以及多空进出场布尔列的新 DataFrame。

    注意：交叉在当前 K 线收盘后才能确认，所以信号统一向后移动一根 K 线。
    """
    # 调用方不传参数时创建默认的 20/50/200 配置。
    parameters = parameters or EmaTrendParameters()
    if "close" not in bars.columns:
        raise ValueError("bars must contain a 'close' column")
    if bars.empty:
        raise ValueError("bars cannot be empty")
    if not bars.index.is_monotonic_increasing or bars.index.has_duplicates:
        raise ValueError("bars index must be sorted and unique")

    # 强制把收盘价转换为数值，字符串或空值异常会在策略入口尽早暴露。
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    # ewm(...).mean() 计算递归 EMA；min_periods 确保预热期不足时不产生信号。
    fast = close.ewm(
        span=parameters.fast_period, adjust=False, min_periods=parameters.fast_period
    ).mean()
    slow = close.ewm(
        span=parameters.slow_period, adjust=False, min_periods=parameters.slow_period
    ).mean()
    trend = close.ewm(
        span=parameters.trend_period, adjust=False, min_periods=parameters.trend_period
    ).mean()

    # 当前快线在慢线上方、上一根不在上方，表示刚刚发生金叉。
    crossed_above = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    # 当前快线在慢线下方、上一根不在下方，表示刚刚发生死叉。
    crossed_below = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    if parameters.entry_mode == "cross":
        # 原始交叉模式要求价格位于EMA200趋势方向，且快慢线斜率同向。
        raw_long_entry = (
            crossed_above & (close > trend) & (fast > fast.shift(1)) & (slow > slow.shift(1))
        )
        raw_short_entry = (
            crossed_below & (close < trend) & (fast < fast.shift(1)) & (slow < slow.shift(1))
        )
    elif parameters.entry_mode == "pullback":
        required_pullback_columns = {"open", "high", "low"}
        missing_pullback_columns = required_pullback_columns - set(bars.columns)
        if missing_pullback_columns:
            raise ValueError(
                f"pullback bars missing required columns: {sorted(missing_pullback_columns)}"
            )
        open_price = pd.to_numeric(bars["open"], errors="raise").astype(float)
        high = pd.to_numeric(bars["high"], errors="raise").astype(float)
        low = pd.to_numeric(bars["low"], errors="raise").astype(float)
        long_alignment = (
            (fast > slow) & (slow > trend) & (fast > fast.shift(1)) & (slow > slow.shift(1))
        )
        short_alignment = (
            (fast < slow) & (slow < trend) & (fast < fast.shift(1)) & (slow < slow.shift(1))
        )
        # rolling只查看当前及之前的已收盘K线，记录近期是否触碰过EMA20。
        recent_long_touch = (
            (low <= fast)
            .rolling(
                parameters.pullback_lookback,
                min_periods=1,
            )
            .max()
            .astype(bool)
        )
        recent_short_touch = (
            (high >= fast)
            .rolling(
                parameters.pullback_lookback,
                min_periods=1,
            )
            .max()
            .astype(bool)
        )
        # 收盘重新越过EMA20并且K线方向一致，表示回踩后恢复；下一根才会实际开仓。
        long_recovery = (close > fast) & (close.shift(1) <= fast.shift(1)) & (close > open_price)
        short_recovery = (close < fast) & (close.shift(1) >= fast.shift(1)) & (close < open_price)
        raw_long_entry = long_alignment & recent_long_touch & long_recovery
        raw_short_entry = short_alignment & recent_short_touch & short_recovery
    elif parameters.entry_mode == "ema12_pullback":
        required_pullback_columns = {"open", "high", "low"}
        missing_pullback_columns = required_pullback_columns - set(bars.columns)
        if missing_pullback_columns:
            raise ValueError(
                f"EMA12 pullback bars missing required columns: "
                f"{sorted(missing_pullback_columns)}"
            )
        open_price = pd.to_numeric(bars["open"], errors="raise").astype(float)
        high = pd.to_numeric(bars["high"], errors="raise").astype(float)
        low = pd.to_numeric(bars["low"], errors="raise").astype(float)

        # 原系统使用固定美元点数判断回踩深度和离开距离，这会让BTC与ETH承担完全不同风险。
        # 这里调用真实波幅计算，并用ATR比例归一化，使两种合约可以在同一风控口径下研究。
        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.ewm(span=14, adjust=False, min_periods=14).mean()

        # EMA12必须位于EMA144和EMA169同一侧，并且最近五根仍在向同一方向运行。
        slope_reference = fast.shift(parameters.pullback_slope_lookback)
        long_alignment = (fast > slow) & (fast > trend) & (fast > slope_reference)
        short_alignment = (fast < slow) & (fast < trend) & (fast < slope_reference)

        # “真实触碰”要求K线价格范围碰到EMA12，同时限制穿透深度，排除已经破坏结构的深回撤。
        long_touch = (low <= fast) & (
            (fast - low) <= atr * parameters.pullback_max_penetration_atr
        )
        short_touch = (high >= fast) & (
            (high - fast) <= atr * parameters.pullback_max_penetration_atr
        )
        recent_long_touch = (
            long_touch.rolling(parameters.pullback_lookback, min_periods=1).max().astype(bool)
        )
        recent_short_touch = (
            short_touch.rolling(parameters.pullback_lookback, min_periods=1).max().astype(bool)
        )

        # 收盘重新离开EMA12达到最小ATR距离才确认恢复；实际成交仍统一放到下一根开盘。
        long_departure = (close - fast) >= atr * parameters.pullback_min_departure_atr
        short_departure = (fast - close) >= atr * parameters.pullback_min_departure_atr
        raw_long_entry = (
            long_alignment & recent_long_touch & long_departure & (close > open_price)
        )
        raw_short_entry = (
            short_alignment & recent_short_touch & short_departure & (close < open_price)
        )
    else:
        required_breakout_columns = {"high", "low"}
        missing_breakout_columns = required_breakout_columns - set(bars.columns)
        if missing_breakout_columns:
            raise ValueError(
                f"breakout bars missing required columns: {sorted(missing_breakout_columns)}"
            )
        high = pd.to_numeric(bars["high"], errors="raise").astype(float)
        low = pd.to_numeric(bars["low"], errors="raise").astype(float)
        # pullback_lookback在旧数据库参数结构中是通用“入场观察窗口”；突破模式复用该字段，
        # 避免增加无法由现有准入和部署代码完整恢复的新参数。
        lookback = parameters.pullback_lookback
        # 先shift再rolling，确保本根最高/最低价没有参与它自己需要突破的历史区间。
        previous_high = high.shift(1).rolling(lookback, min_periods=lookback).max()
        previous_low = low.shift(1).rolling(lookback, min_periods=lookback).min()
        long_alignment = (fast > slow) & (close > trend)
        short_alignment = (fast < slow) & (close < trend)
        # 只有收盘真正越过此前区间才确认突破，随后仍统一在下一根K线开盘执行。
        raw_long_entry = long_alignment & (close > previous_high)
        raw_short_entry = short_alignment & (close < previous_low)

    if parameters.trend_slope_lookback > 0:
        # 与若干根之前的EMA200比较，要求大趋势已经持续转向，而非只看本根瞬时变化。
        # 当前价格位于EMA上方会令EMA本根自然上升，所以只比较前一根没有新增信息。
        earlier_trend = trend.shift(parameters.trend_slope_lookback)
        raw_long_entry &= trend > earlier_trend
        raw_short_entry &= trend < earlier_trend

    # 新建结果表而不修改调用方传入的原始 K 线，避免回测数据被意外污染。
    result = pd.DataFrame(index=bars.index)
    result["close"] = close
    result["ema_fast"] = fast
    result["ema_slow"] = slow
    result["ema_trend"] = trend
    # 收盘时观察到的信号只能在下一根 K 线执行，shift(1) 用于杜绝未来数据。
    result["long_entry"] = raw_long_entry.shift(1, fill_value=False).astype(bool)
    result["short_entry"] = raw_short_entry.shift(1, fill_value=False).astype(bool)
    result["long_exit"] = crossed_below.shift(1, fill_value=False).astype(bool)
    result["short_exit"] = crossed_above.shift(1, fill_value=False).astype(bool)
    return result
