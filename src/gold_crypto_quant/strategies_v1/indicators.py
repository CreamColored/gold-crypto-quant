"""震荡 v1.0 与顺势 v1.0 共用的形态识别。

和 V5.8 里那套的区别都写在各函数的注释里，主要是三条：
浪形有最小根数门槛（L1R1）、趋势按高低点双向递增判定（L1R4/R5）、
箱体边沿按水平触碰聚类而不是布林带轨道（L8R2/R3）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


@dataclass(frozen=True, slots=True)
class MacdFrame:
    """收盘价、DIF、DEA、柱值四条对齐的序列。"""

    close: pd.Series
    dif: pd.Series
    dea: pd.Series
    histogram: pd.Series


def macd_frame(bars: pd.DataFrame) -> MacdFrame:
    """标准 MACD(12,26,9)。柱值取 (DIF-DEA)，不乘 2——只比较相对大小，倍数无关。"""
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return MacdFrame(close=close, dif=dif, dea=dea, histogram=dif - dea)


def histogram_waves(histogram: pd.Series, *, min_bars: int = 2) -> list[tuple[int, int, int]]:
    """把 MACD 柱切成浪形，返回 [(方向, 起下标, 止下标), ...]。

    方向 +1 为红柱区、-1 为绿柱区，下标闭区间。

    与 V5.8 的关键差别：``min_bars`` 门槛。L1R1 明确说"最少要有两根线才能
    当作是一个浪形，只有一根时不单独计为一个浪形，而视为前一段的延续"。
    V5.8 的 _histogram_groups 对任何长度都算一组，单根柱的噪声会被当独立浪形。

    不够 min_bars 的段被并进前一段——"视为前一段的延续"就是这个意思；
    前面没有段可并时直接丢弃。
    """
    values = histogram.to_numpy(dtype=float)
    raw: list[tuple[int, int, int]] = []
    start = 0
    for index in range(1, len(values) + 1):
        ended = index == len(values) or (
            np.sign(values[index]) != np.sign(values[index - 1]) and values[index] != 0
        )
        if ended:
            direction = int(np.sign(values[start])) or (raw[-1][0] if raw else 1)
            raw.append((direction, start, index - 1))
            start = index
    merged: list[tuple[int, int, int]] = []
    for direction, first, last in raw:
        if last - first + 1 >= min_bars:
            merged.append((direction, first, last))
        elif merged:
            # 不足门槛：并入前一浪，延长它的终点。
            prev_dir, prev_first, _ = merged[-1]
            merged[-1] = (prev_dir, prev_first, last)
    return merged


@dataclass(frozen=True, slots=True)
class TrendVerdict:
    """趋势判定结果。direction: +1 上升、-1 下降、0 无趋势。"""

    direction: int
    wave_count: int
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    reason: str


def classify_trend(
    bars: pd.DataFrame, *, min_waves: int = 3, min_wave_bars: int = 2, lookback: int = 150
) -> TrendVerdict:
    """按 L1R3/R4/R5 判趋势。

    L1R4 上升趋势 = 高点收盘价递增 **且** 低点收盘价递增；L1R5 下降反之。
    两个条件必须同时成立——只有高点递增而低点不递增，是扩张而不是上升趋势。
    用收盘价而不是影线，与 L2R3 一致。
    """
    window = bars.tail(lookback)
    if len(window) < MACD_SLOW + min_waves * min_wave_bars:
        return TrendVerdict(0, 0, (), (), "K线不足以判定浪形")
    frame = macd_frame(window)
    waves = histogram_waves(frame.histogram, min_bars=min_wave_bars)
    if len(waves) < min_waves:
        return TrendVerdict(0, len(waves), (), (), f"浪形只有{len(waves)}个，不足{min_waves}个")

    close = frame.close.to_numpy(dtype=float)
    recent = waves[-min_waves:]
    # 每一浪取其收盘价的极值：红柱区取最高收盘、绿柱区取最低收盘。
    highs = tuple(float(close[first : last + 1].max()) for _, first, last in recent)
    lows = tuple(float(close[first : last + 1].min()) for _, first, last in recent)

    rising_highs = all(b > a for a, b in zip(highs, highs[1:], strict=False))
    rising_lows = all(b > a for a, b in zip(lows, lows[1:], strict=False))
    falling_highs = all(b < a for a, b in zip(highs, highs[1:], strict=False))
    falling_lows = all(b < a for a, b in zip(lows, lows[1:], strict=False))

    if rising_highs and rising_lows:
        return TrendVerdict(1, len(waves), highs, lows, "高点与低点同时递增")
    if falling_highs and falling_lows:
        return TrendVerdict(-1, len(waves), highs, lows, "高点与低点同时递减")
    return TrendVerdict(0, len(waves), highs, lows, "高低点未同向，不构成趋势")


def swing_points(
    bars: pd.DataFrame, *, window: int = 2, use_body: bool = True
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """找摆动高点与低点，返回 ([(下标, 价), ...高点], [...低点])。

    ``use_body`` 为真时用实体（max/min of open, close）而不是 high/low。
    L8R2 要求"瞬间的插针剔除"——插针是影线，用实体就自动剔掉了，
    这比另外定义一个"插针"阈值可靠，因为视频并没有给插针的量化标准。
    """
    if use_body:
        highs = pd.concat([bars["open"], bars["close"]], axis=1).max(axis=1).to_numpy(dtype=float)
        lows = pd.concat([bars["open"], bars["close"]], axis=1).min(axis=1).to_numpy(dtype=float)
    else:
        highs = pd.to_numeric(bars["high"], errors="raise").to_numpy(dtype=float)
        lows = pd.to_numeric(bars["low"], errors="raise").to_numpy(dtype=float)

    peak: list[tuple[int, float]] = []
    trough: list[tuple[int, float]] = []
    for index in range(window, len(bars) - window):
        left, right = slice(index - window, index), slice(index + 1, index + 1 + window)
        if highs[index] >= highs[left].max() and highs[index] >= highs[right].max():
            peak.append((index, float(highs[index])))
        if lows[index] <= lows[left].min() and lows[index] <= lows[right].min():
            trough.append((index, float(lows[index])))
    return peak, trough


def cluster_level(
    points: list[tuple[int, float]], *, tolerance: float, min_count: int
) -> tuple[float, int] | None:
    """把价格相近的摆动点聚成一条水平线，返回 (价位, 触碰次数)。

    L8R2/R3 要求"至少 2 次高点位于同一价格附近并作水平连线"。
    "附近"视频没给数字，由 ``tolerance``（相对价格）填。

    选哪一簇：触碰次数最多的；并列时取更靠近当前的那一簇，
    因为旧的水平线可能早已失效。
    """
    if len(points) < min_count:
        return None
    best: tuple[float, int, int] | None = None  # (价位, 次数, 最新下标)
    for _, anchor in points:
        members = [(i, p) for i, p in points if abs(p - anchor) <= anchor * tolerance]
        if len(members) < min_count:
            continue
        level = float(np.mean([p for _, p in members]))
        latest = max(i for i, _ in members)
        candidate = (level, len(members), latest)
        if best is None or (candidate[1], candidate[2]) > (best[1], best[2]):
            best = candidate
    return (best[0], best[1]) if best else None


@dataclass(frozen=True, slots=True)
class Box:
    """一个箱体：水平上下沿、各自的触碰次数。"""

    upper: float
    lower: float
    upper_touches: int
    lower_touches: int

    @property
    def height(self) -> float:
        return self.upper - self.lower

    @property
    def middle(self) -> float:
        return (self.upper + self.lower) / 2.0

    @property
    def touches(self) -> int:
        return self.upper_touches + self.lower_touches


def detect_box(
    bars: pd.DataFrame,
    *,
    lookback: int = 200,
    swing_window: int = 2,
    tolerance: float = 0.0015,
    min_touches: int = 2,
    min_height: float = 0.004,
    max_height: float = 0.060,
    use_body: bool = True,
) -> Box | None:
    """按 L8R1–R4 找箱体；找不到返回 None。

    与 V5.8 的根本差别：这里的上下沿是历史上真实被反复拒绝的水平价位，
    布林带轨道只是统计带，价格靠近上轨并不代表那里有阻力。
    """
    window = bars.tail(lookback)
    if len(window) < swing_window * 2 + min_touches * 2:
        return None
    peaks, troughs = swing_points(window, window=swing_window, use_body=use_body)
    upper = cluster_level(peaks, tolerance=tolerance, min_count=min_touches)
    lower = cluster_level(troughs, tolerance=tolerance, min_count=min_touches)
    if upper is None or lower is None or upper[0] <= lower[0]:
        return None
    box = Box(upper[0], lower[0], upper[1], lower[1])
    relative = box.height / box.middle if box.middle else 0.0
    if not min_height <= relative <= max_height:
        return None
    return box


def volume_ratio(bars: pd.DataFrame, *, span: int, baseline: int) -> float:
    """最近 ``span`` 根的均量 ÷ 再往前 ``baseline`` 根的均量。

    L7 和 L8R11 全篇用的都是"倍量""1.5 至 2 倍"这种相对口径，所以这里也只给比值。
    数据缺 volume 或基准为 0 时返回 1.0（中性），不让缺数据变成信号。
    """
    if "volume" not in bars.columns or len(bars) < span + baseline:
        return 1.0
    values = pd.to_numeric(bars["volume"], errors="coerce").astype(float)
    recent = values.iloc[-span:].mean()
    prior = values.iloc[-(span + baseline) : -span].mean()
    if not prior or not np.isfinite(prior) or not np.isfinite(recent):
        return 1.0
    return float(recent / prior)


def average_true_range(bars: pd.DataFrame, period: int = 14) -> float:
    """最新一根的 ATR。止损缓冲用它换算成价格距离（L13R8）。"""
    high = pd.to_numeric(bars["high"], errors="raise").astype(float)
    low = pd.to_numeric(bars["low"], errors="raise").astype(float)
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    prev = close.shift(1)
    ranges = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = ranges.ewm(alpha=1 / period, adjust=False).mean()
    value = float(atr.iloc[-1])
    return value if np.isfinite(value) and value > 0 else 0.0
