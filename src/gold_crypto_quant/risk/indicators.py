"""风险和市场状态指标计算。"""

import numpy as np

import pandas as pd


def average_true_range(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """根据最高价、最低价和收盘价计算 Wilder ATR。

    Args:
        bars: 以时间升序排列的 K 线，必须包含 ``high``、``low``、``close``。
        period: ATR 平滑周期，默认 14。

    Returns:
        与输入索引对齐的 ATR 序列；预热期不足时返回 NaN。
    """
    if period < 2:
        raise ValueError("period must be at least 2")
    missing = {"high", "low", "close"}.difference(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")

    # 明确转换为数值；发现脏数据时立即抛错，避免错误价格进入止损计算。
    high = pd.to_numeric(bars["high"], errors="raise").astype(float)
    low = pd.to_numeric(bars["low"], errors="raise").astype(float)
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)

    # shift(1) 取得上一根已收盘 K 线的收盘价，不会读取未来数据。
    previous_close = close.shift(1)
    # 真实波幅取三种跨度的最大值，用于覆盖跳空造成的价格变化。
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)

    # Wilder ATR 等价于 alpha=1/period 的递归指数平滑；adjust=False 使用递推形式。
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def average_directional_index(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算Wilder ADX，用于判断趋势强度而不判断涨跌方向。"""
    if period < 2:
        raise ValueError("period must be at least 2")
    missing = {"high", "low", "close"}.difference(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")

    high = pd.to_numeric(bars["high"], errors="raise").astype(float)
    low = pd.to_numeric(bars["low"], errors="raise").astype(float)
    upward_move = high.diff()
    downward_move = -low.diff()
    # 只保留更强且为正的方向移动，避免同一根K线同时计入+DM和-DM。
    positive_dm = upward_move.where((upward_move > downward_move) & (upward_move > 0), 0.0)
    negative_dm = downward_move.where((downward_move > upward_move) & (downward_move > 0), 0.0)

    # 调用统一ATR方法作为方向指标分母，确保真实波幅算法只有一个实现来源。
    atr = average_true_range(bars, period)
    positive_smoothed = positive_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    negative_smoothed = negative_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    positive_di = 100.0 * positive_smoothed / atr
    negative_di = 100.0 * negative_smoothed / atr
    # 使用浮点NaN而不是pd.NA，避免完全静止行情把序列提升为object后使ewm无法计算。
    denominator = (positive_di + negative_di).where(
        (positive_di + negative_di) != 0.0,
        float("nan"),
    )
    directional_index = 100.0 * (positive_di - negative_di).abs() / denominator
    # ADX是DX的第二次Wilder平滑，因此需要比ATR更长的预热区间。
    return directional_index.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def choppiness_index(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算无方向的 CHOP；值越高表示窗口内路径相对净位移越曲折。

    公式与 TradingView 内置 CHOP 一致：窗口真实波幅之和除以窗口最高最低差，
    再用以 ``period`` 为底的对数归一到 0～100。只读取当前及过去K线。
    """
    if period < 2:
        raise ValueError("period must be at least 2")
    missing = {"high", "low", "close"}.difference(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    high = pd.to_numeric(bars["high"], errors="raise").astype(float)
    low = pd.to_numeric(bars["low"], errors="raise").astype(float)
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    travelled = true_range.rolling(period).sum()
    displacement = (high.rolling(period).max() - low.rolling(period).min()).replace(0.0, np.nan)
    return 100.0 * np.log10(travelled / displacement) / np.log10(period)
