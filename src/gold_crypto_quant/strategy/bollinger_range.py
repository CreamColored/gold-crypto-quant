"""同周期识别与触轨的布林带箱体策略，并提供V6生命周期证据。"""

from dataclasses import asdict, dataclass, replace
from decimal import Decimal

import numpy as np
import pandas as pd

from gold_crypto_quant.risk.indicators import (
    average_directional_index,
    average_true_range,
    choppiness_index,
)
from gold_crypto_quant.strategy.live_signals import NextBarSignalDecision, SignalAction


# 模块级总开关。回放做 A/B 对照时设 False 即可复现 V5.8 的箱体判定，
# 不必改参数构造链。线上保持 True。
REGIME_FILTER_ENABLED = True

# 震荡判据阈值的运行时覆盖。为空则用 parameters_for_same_timeframe 的默认值。
# 存在的意义：V5.9 的默认阈值是从三段人工标注拟合出来的，不能作为研究起点；
# 网格扫描时由训练集自行选择，覆盖走这里，不改默认值。
# 学习出来的震荡识别器。默认 None＝走原来的规则判据，行为完全不变。
# 研究时注入 {(品种, 周期): 布尔Series}，**整体替换**规则判据，而不是再加一层过滤——
# 加一层只能让箱体更少，替换才能测出"换一批箱体"到底好不好。
# 保留 ~breakout：连续两根收盘出轨是客观的结构事实，不属于识别启发式。
BOX_CANDIDATE_OVERRIDE: dict | None = None
REGIME_OVERRIDE: dict | None = None


@dataclass(frozen=True, slots=True)
class BollingerRangeParameters:
    """首版严格震荡策略的全部可审计参数。"""

    bollinger_period: int = 20
    bollinger_std: float = 2.0
    maximum_band_drift: float = 2.5
    maximum_bandwidth_growth: float = 0.10
    adx_period: int = 14
    maximum_adx: float = 20.0
    volume_period: int = 20
    maximum_volume_ratio: float = 2.0
    fixed_stop_distance: float = 5.0
    middle_exit_fraction: float = 0.5
    cooldown_bars_after_stop: int = 3
    maximum_stops_per_regime: int = 2
    allow_immediate_reversal: bool = False
    rotation_flat_window: int = 3
    minimum_bandwidth: float = 5.0

    # ---- V5.9 震荡识别（四条合取）----
    # 依据是用户在 2026-09-02～09-04 三段行情上的人工标注：
    # 震荡段的带宽只有非震荡段的 0.4–0.5 倍、中轨穿越次数 1.7–2.1 倍、
    # 带宽变化为负（收口）而非震荡段是 +71%～+130%（开口）、MACD 柱体 0.5–0.7 倍。
    # 阈值由标注数据拟合得出，不是拍脑袋定的。
    regime_enabled: bool = True
    regime_window: int = 20
    """判据的滚动窗口。V5.8 的"走平"只看连续 3 根，单边行情中途歇 3 根就能通过；
    标注出的震荡段动辄几十根，窗口要匹配这个尺度。"""

    maximum_relative_width: float = 1.2
    """带宽上限，相对中轨的百分比。V5.8 只有下限没有上限——
    但震荡的特征是带宽**窄**，宽带的单边行情不该被当成箱体。"""

    minimum_middle_crossings: int = 3
    """窗口内收盘价穿越中轨的最少次数。单边行情贴着一侧轨走、很少穿中轨；
    震荡是"站上中轨然后上下摇摆"。这是 V5.8 完全没有的维度。"""

    maximum_width_growth: float = 50.0
    """窗口内带宽增长上限（百分比）。开口意味着单边行情正在展开。
    标注数据里震荡段是负值（收口），非震荡段 +71% 起。"""

    maximum_macd_histogram: float = 1.2
    """MACD 柱体绝对值均值上限（相对价格的千分比）。动能有方向性积累就不是震荡。"""

    lifecycle_confirmation_bars: int = 2
    lifecycle_maximum_age_bars: int = 96
    minimum_range_evidence: float = 0.55
    maximum_breakout_risk: float = 0.50

    def __post_init__(self) -> None:
        if self.bollinger_period < 2 or self.regime_window < 2:
            raise ValueError("Bollinger period and regime window must be at least two")
        if self.bollinger_std <= 0 or self.maximum_band_drift <= 0:
            raise ValueError("Bollinger width and drift settings must be positive")
        if not 0 <= self.maximum_bandwidth_growth <= 1:
            raise ValueError("maximum bandwidth growth must be in [0, 1]")
        if self.adx_period < 2 or self.maximum_adx <= 0:
            raise ValueError("ADX settings must be positive")
        if self.volume_period < 2 or self.maximum_volume_ratio <= 1:
            raise ValueError("volume settings are invalid")
        if self.fixed_stop_distance <= 0:
            raise ValueError("fixed stop distance must be positive")
        if not 0 < self.middle_exit_fraction <= 1:
            raise ValueError("middle exit fraction must be in (0, 1]")
        if self.cooldown_bars_after_stop < 0 or self.maximum_stops_per_regime < 1:
            raise ValueError("stop lock settings are invalid")
        if self.rotation_flat_window < 3 or self.minimum_bandwidth <= 0:
            raise ValueError("rotation box settings are invalid")
        if self.regime_window < 5:
            raise ValueError("震荡判据窗口至少 5 根")
        if self.maximum_relative_width <= 0:
            raise ValueError("带宽上限必须为正")
        if self.minimum_middle_crossings < 0:
            raise ValueError("中轨穿越次数不能为负")
        if self.maximum_macd_histogram <= 0:
            raise ValueError("柱体上限必须为正")
        if self.lifecycle_confirmation_bars < 1 or self.lifecycle_maximum_age_bars < 2:
            raise ValueError("箱体生命周期参数无效")
        if not 0 <= self.minimum_range_evidence <= 1:
            raise ValueError("震荡证据分数必须在0到1之间")
        if not 0 <= self.maximum_breakout_risk <= 1:
            raise ValueError("突破风险分数必须在0到1之间")

    def to_dict(self) -> dict[str, object]:
        """转换为可写入策略运行和准入记录的普通字典。"""
        return asdict(self)


def calculate_bollinger_bands(
    bars: pd.DataFrame,
    parameters: BollingerRangeParameters,
) -> pd.DataFrame:
    """计算布林带、带宽、ADX、成交量比例和ATR，结果与输入索引严格对齐。"""
    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    volume = pd.to_numeric(bars["volume"], errors="raise").astype(float)
    middle = close.rolling(parameters.bollinger_period).mean()
    deviation = close.rolling(parameters.bollinger_period).std(ddof=0)
    upper = middle + parameters.bollinger_std * deviation
    lower = middle - parameters.bollinger_std * deviation
    volume_average = volume.rolling(parameters.volume_period).mean()
    return pd.DataFrame(
        {
            "bb_upper": upper,
            "bb_middle": middle,
            "bb_lower": lower,
            "bb_width": upper - lower,
            "adx": average_directional_index(bars, parameters.adx_period),
            "volume_ratio": volume / volume_average.replace(0.0, pd.NA),
            "atr": average_true_range(bars, 14),
        },
        index=bars.index,
    )


def build_strict_sideways_regime(
    bars_15m: pd.DataFrame,
    parameters: BollingerRangeParameters,
) -> pd.DataFrame:
    """把主观的“15分钟三轨连续走平”转换成完全可复现的布尔条件。"""
    indicators = calculate_bollinger_bands(bars_15m, parameters)
    window = parameters.regime_window
    drift_allowed = pd.Series(True, index=bars_15m.index)
    for column in ("bb_upper", "bb_middle", "bb_lower"):
        # 每条轨道在完整观察窗口中的最高与最低差都不能超过固定点数阈值。
        drift = indicators[column].rolling(window).max() - indicators[column].rolling(window).min()
        drift_allowed &= drift <= parameters.maximum_band_drift
    previous_width = indicators["bb_width"].shift(window - 1)
    width_growth = indicators["bb_width"] / previous_width.replace(0.0, pd.NA) - 1.0
    no_opening = width_growth <= parameters.maximum_bandwidth_growth
    inside = bars_15m["close"].between(
        indicators["bb_lower"], indicators["bb_upper"], inclusive="both"
    )
    # 连续两根同侧收在轨道外视为突破；rolling只读取当前及过去已经收盘的K线。
    previous_inside = inside.shift(1).fillna(False).astype(bool)
    two_closes_outside = ((~inside.astype(bool)) & (~previous_inside)).astype(bool)
    normal_volume = indicators["volume_ratio"] <= parameters.maximum_volume_ratio
    sideways = (
        drift_allowed
        & no_opening
        & (indicators["adx"] < parameters.maximum_adx)
        & ~two_closes_outside
        & normal_volume
    ).fillna(False)
    result = indicators.copy()
    # 保存主周期收盘价，供映射到5分钟后判断位于中轨上半区还是下半区。
    result["close"] = bars_15m["close"].astype(float)
    result["sideways"] = sideways
    # 连续True区间获得稳定编号，供“两次止损后暂停当前区间”使用。
    result["regime_id"] = (sideways.ne(sideways.shift()).cumsum()).where(sideways)
    return result


def align_completed_regime_to_5m(
    bars_5m: pd.DataFrame,
    bars_15m: pd.DataFrame,
    parameters: BollingerRangeParameters,
) -> pd.DataFrame:
    """把已完整结束的15分钟状态映射到每根5分钟K线，杜绝高周期未来数据。"""
    if bars_5m.index.tz is None or bars_15m.index.tz is None:
        raise ValueError("market bars must use timezone-aware indexes")
    regime = build_strict_sideways_regime(bars_15m, parameters).copy()
    # 15分钟K线在开盘后15分钟才可见，映射键必须使用完成时间而不是开盘时间。
    regime["available_at"] = regime.index + pd.Timedelta(minutes=15)
    left = pd.DataFrame(
        {"source_5m_time": bars_5m.index, "available_at": bars_5m.index + pd.Timedelta(minutes=5)}
    )
    right = regime.reset_index(drop=False).rename(columns={regime.index.name or "index": "source_15m_time"})
    aligned = pd.merge_asof(
        left.sort_values("available_at"),
        right.sort_values("available_at"),
        on="available_at",
        direction="backward",
    ).set_index("source_5m_time")
    aligned.index.name = bars_5m.index.name
    return aligned.reindex(bars_5m.index)


def build_bollinger_entry_context(
    bars_5m: pd.DataFrame,
    bars_15m: pd.DataFrame,
    parameters: BollingerRangeParameters | None = None,
) -> pd.DataFrame:
    """计算5分钟触轨收回信号及其对应的15分钟严格震荡上下文。"""
    parameters = parameters or BollingerRangeParameters()
    if bars_5m.empty or bars_15m.empty:
        raise ValueError("5m and 15m bars cannot be empty")
    indicators_5m = calculate_bollinger_bands(bars_5m, parameters)
    regime = align_completed_regime_to_5m(bars_5m, bars_15m, parameters)
    close_5m = bars_5m["close"].astype(float)
    low_5m = bars_5m["low"].astype(float)
    high_5m = bars_5m["high"].astype(float)
    lower_not_falling = indicators_5m["bb_lower"].diff() >= 0.0
    upper_not_rising = indicators_5m["bb_upper"].diff() <= 0.0
    normal_volume = indicators_5m["volume_ratio"] <= parameters.maximum_volume_ratio
    strict = regime["sideways"].fillna(False).astype(bool)
    lower_half = regime["close"].le(regime["bb_middle"])
    upper_half = regime["close"].ge(regime["bb_middle"])
    long_entry = (
        strict
        & lower_half
        & low_5m.le(indicators_5m["bb_lower"])
        & close_5m.gt(indicators_5m["bb_lower"])
        & lower_not_falling
        & normal_volume
    ).fillna(False)
    short_entry = (
        strict
        & upper_half
        & high_5m.ge(indicators_5m["bb_upper"])
        & close_5m.lt(indicators_5m["bb_upper"])
        & upper_not_rising
        & normal_volume
    ).fillna(False)
    result = indicators_5m.copy()
    result["sideways"] = strict
    result["regime_id"] = regime["regime_id"]
    result["long_entry"] = long_entry
    result["short_entry"] = short_entry
    return result


def build_same_timeframe_entry_context(
    bars: pd.DataFrame,
    parameters: BollingerRangeParameters | None = None,
) -> pd.DataFrame:
    """在同一个K线周期完成震荡判定、触轨确认和进场条件计算。"""
    parameters = parameters or BollingerRangeParameters()
    if bars.empty:
        raise ValueError("bars cannot be empty")
    # 调用同周期严格震荡识别，当前K线收盘后才会确认状态，交易延迟到下一根开盘。
    regime = build_strict_sideways_regime(bars, parameters)
    close = bars["close"].astype(float)
    low = bars["low"].astype(float)
    high = bars["high"].astype(float)
    normal_volume = regime["volume_ratio"] <= parameters.maximum_volume_ratio
    lower_not_falling = regime["bb_lower"].diff() >= 0.0
    upper_not_rising = regime["bb_upper"].diff() <= 0.0
    lower_half = close <= regime["bb_middle"]
    upper_half = close >= regime["bb_middle"]
    result = regime.copy()
    result["long_entry"] = (
        regime["sideways"]
        & lower_half
        & low.le(regime["bb_lower"])
        & close.gt(regime["bb_lower"])
        & lower_not_falling
        & normal_volume
    ).fillna(False)
    result["short_entry"] = (
        regime["sideways"]
        & upper_half
        & high.ge(regime["bb_upper"])
        & close.lt(regime["bb_upper"])
        & upper_not_rising
        & normal_volume
    ).fillna(False)
    return result


def build_rotation_box_context(
    bars: pd.DataFrame,
    parameters: BollingerRangeParameters | None = None,
    market_key: tuple[str, str] | None = None,
) -> pd.DataFrame:
    """计算15分钟轨道轮转所需的箱体、突破和固定轨道。

    该结果只描述已经收盘的K线。模拟器处理下一根K线时必须读取前一行轨道，
    这样才能在K线开始前挂出上下轨限价单，禁止使用本根收盘后才确定的布林带。
    """
    parameters = parameters or BollingerRangeParameters()
    indicators = calculate_bollinger_bands(bars, parameters)
    flat = pd.Series(True, index=bars.index)
    for column in ("bb_upper", "bb_middle", "bb_lower"):
        # 调用滚动极差，把“连续三根三轨各自移动不超过2.5点”变成可复现条件。
        drift = (
            indicators[column].rolling(parameters.rotation_flat_window).max()
            - indicators[column].rolling(parameters.rotation_flat_window).min()
        )
        flat &= drift <= parameters.maximum_band_drift
    inside = bars["close"].between(
        indicators["bb_lower"], indicators["bb_upper"], inclusive="both"
    )
    # 连续两根收盘位于轨道外才确认突破；单根插针或轻微越轨不立即终止箱体。
    breakout = ((~inside) & (~inside.shift(1).fillna(False).astype(bool))).fillna(False)
    normal_volume = indicators["volume_ratio"] <= parameters.maximum_volume_ratio
    result = indicators.copy()
    box_candidate = (
        flat
        & (indicators["bb_width"] >= parameters.minimum_bandwidth)
        & normal_volume
        & ~breakout
    )

    # V5.9和V6共用的归一化特征先统一计算；关闭V5.9硬过滤时，V6评分仍可工作。
    window = parameters.regime_window
    middle = indicators["bb_middle"]
    relative_width = indicators["bb_width"] / middle * 100.0
    side = np.sign(bars["close"] - middle)
    crossings = (side != side.shift(1)).rolling(window).sum()

    # ---- V5.9 震荡识别 ----
    # V5.8 只判"三轨走平"，实测在人工标注的三段行情上精确率只有 58%（15m）——
    # 它认定的箱体里 42% 是单边行情，策略在那里做高抛低吸，
    # 一年回放里 47% 的交易死于固定保护止损、亏掉 16,062U。
    # 这四条来自标注数据里区分度最强的四个特征。
    if parameters.regime_enabled and REGIME_FILTER_ENABLED:
        # 带宽相对中轨，做成百分比才能跨品种跨价格比较。
        # 中轨穿越：收盘价相对中轨的符号翻转次数。
        # 带宽变化：正为开口（单边展开），负为收口。
        width_growth = (
            relative_width.diff(window) / relative_width.shift(window).replace(0.0, pd.NA) * 100.0
        )
        # MACD 柱体绝对值，按价格归一到千分比。
        fast = bars["close"].ewm(span=12, adjust=False).mean()
        slow = bars["close"].ewm(span=26, adjust=False).mean()
        dif = fast - slow
        histogram = dif - dif.ewm(span=9, adjust=False).mean()
        histogram_scale = (histogram.abs() / bars["close"] * 1000.0).rolling(window).mean()

        box_candidate &= (
            (relative_width <= parameters.maximum_relative_width)
            & (crossings >= parameters.minimum_middle_crossings)
            & (width_growth <= parameters.maximum_width_growth)
            & (histogram_scale <= parameters.maximum_macd_histogram)
        )
        result["relative_width"] = relative_width
        result["middle_crossings"] = crossings
        result["width_growth"] = width_growth
        result["histogram_scale"] = histogram_scale

    if BOX_CANDIDATE_OVERRIDE is not None and market_key is not None:
        learned = BOX_CANDIDATE_OVERRIDE.get(market_key)
        if learned is not None:
            # 整体替换识别判据。对不齐的时间点按 False 处理——宁可漏掉，
            # 不能因为查不到分数就默认放行。
            box_candidate = learned.reindex(bars.index).fillna(False).astype(bool) & ~breakout
    result["box_candidate"] = box_candidate.fillna(False)
    result["breakout"] = breakout.astype(bool)
    result["close"] = bars["close"].astype(float)
    # V6候选特征只负责提供可审计分数；是否冻结、老化或作废由运行时生命周期管理。
    chop = choppiness_index(bars, parameters.adx_period)
    bbw_percentile = relative_width.rolling(252).rank(pct=True) * 100.0
    short_width_growth = relative_width / relative_width.shift(3).replace(0.0, pd.NA) - 1.0
    crossing_score = (crossings / 4.0).clip(0.0, 1.0)
    chop_score = ((chop - 38.2) / (61.8 - 38.2)).clip(0.0, 1.0)
    weak_trend_score = ((25.0 - indicators["adx"]) / 10.0).clip(0.0, 1.0)
    range_evidence = (
        flat.astype(float) * 0.35
        + crossing_score * 0.25
        + chop_score * 0.20
        + weak_trend_score * 0.20
    )
    breakout_risk = (
        (bbw_percentile <= 10.0).astype(float) * 0.25
        + (short_width_growth > 0.20).astype(float) * 0.30
        + (indicators["adx"] >= 25.0).astype(float) * 0.25
        + (indicators["volume_ratio"] > 1.5).astype(float) * 0.20
    ).clip(0.0, 1.0)
    result["choppiness"] = chop
    result["bbw_percentile"] = bbw_percentile
    result["short_width_growth"] = short_width_growth
    result["range_evidence_score"] = range_evidence
    result["breakout_risk_score"] = breakout_risk
    return result


def parameters_for_same_timeframe(interval: str) -> BollingerRangeParameters:
    """返回各同周期模式的三轨连续走平点数阈值。"""
    # 震荡判据的阈值按周期分别拟合。周期越大带宽天然越宽，用同一个数会
    # 让大周期永远不合格。1h 的人工标注只有 9 根、拟合不可靠，沿用 30m 的值。
    def _apply(base: "BollingerRangeParameters") -> "BollingerRangeParameters":
        if not REGIME_OVERRIDE:
            return base
        return replace(base, **REGIME_OVERRIDE)

    if interval == "15m":
        return _apply(BollingerRangeParameters(
            maximum_band_drift=2.5,
            maximum_relative_width=1.2,
            maximum_macd_histogram=1.2,
        ))
    if interval == "5m":
        return _apply(BollingerRangeParameters(
            maximum_band_drift=1.0,
            maximum_relative_width=0.9,
            maximum_macd_histogram=0.9,
        ))
    if interval in {"30m", "1h"}:
        return _apply(BollingerRangeParameters(
            maximum_band_drift=3.0,
            maximum_relative_width=1.8,
            maximum_macd_histogram=1.7,
        ))
    raise ValueError("same-timeframe Bollinger strategy supports 5m, 15m, 30m and 1h")


def evaluate_same_timeframe_next_bar_signal(
    bars: pd.DataFrame,
    interval: str,
    *,
    parameters: BollingerRangeParameters | None = None,
) -> NextBarSignalDecision:
    """把同周期最新收盘确认转换为该周期下一根开盘的可审计信号。"""
    parameters = parameters or parameters_for_same_timeframe(interval)
    context = build_same_timeframe_entry_context(bars, parameters)
    latest = context.iloc[-1]
    duration = pd.Timedelta(minutes=5 if interval == "5m" else 15)
    actions: list[SignalAction] = []
    if bool(latest["long_entry"]):
        actions.append(SignalAction("LONG", "ENTRY"))
    if bool(latest["short_entry"]):
        actions.append(SignalAction("SHORT", "ENTRY"))

    def optional_float(value: object) -> float | None:
        """把预热期NaN转换为JSON可保存值。"""
        return float(value) if pd.notna(value) else None

    latest_time = bars.index[-1]
    return NextBarSignalDecision(
        bar_time=(latest_time + duration).to_pydatetime(),
        signal_price=Decimal(str(float(bars.iloc[-1]["close"]))),
        actions=tuple(actions),
        payload={
            "source_bar_open_time": latest_time.isoformat(),
            "execution_rule": f"NEXT_{interval.upper()}_OPEN_AFTER_CONFIRMED_CLOSE",
            "strategy": "BOLLINGER_RANGE_SAME_TIMEFRAME",
            "signal_interval": interval,
            "sideways": bool(latest["sideways"]),
            "regime_id": optional_float(latest["regime_id"]),
            "bb_upper": optional_float(latest["bb_upper"]),
            "bb_middle": optional_float(latest["bb_middle"]),
            "bb_lower": optional_float(latest["bb_lower"]),
            "adx": optional_float(latest["adx"]),
            "volume_ratio": optional_float(latest["volume_ratio"]),
            "parameters": parameters.to_dict(),
        },
    )


def evaluate_bollinger_next_bar_signal(
    bars_5m: pd.DataFrame,
    bars_15m: pd.DataFrame,
    *,
    parameters: BollingerRangeParameters | None = None,
) -> NextBarSignalDecision:
    """使用最新已收盘5分钟K线生成下一根开盘可执行的布林带入场决定。"""
    parameters = parameters or BollingerRangeParameters()
    context = build_bollinger_entry_context(bars_5m, bars_15m, parameters)
    latest = context.iloc[-1]
    latest_time = bars_5m.index[-1]
    actions: list[SignalAction] = []
    if bool(latest["long_entry"]):
        actions.append(SignalAction("LONG", "ENTRY"))
    if bool(latest["short_entry"]):
        actions.append(SignalAction("SHORT", "ENTRY"))

    def optional_float(value: object) -> float | None:
        """把预热期NaN安全转换为可保存的JSON值。"""
        return float(value) if pd.notna(value) else None

    return NextBarSignalDecision(
        bar_time=(latest_time + pd.Timedelta(minutes=5)).to_pydatetime(),
        signal_price=Decimal(str(float(bars_5m.iloc[-1]["close"]))),
        actions=tuple(actions),
        payload={
            "source_bar_open_time": latest_time.isoformat(),
            "execution_rule": "NEXT_5M_OPEN_AFTER_CONFIRMED_CLOSE",
            "strategy": "BOLLINGER_RANGE",
            "sideways": bool(latest["sideways"]),
            "regime_id": optional_float(latest["regime_id"]),
            "bb_upper": optional_float(latest["bb_upper"]),
            "bb_middle": optional_float(latest["bb_middle"]),
            "bb_lower": optional_float(latest["bb_lower"]),
            "adx_5m": optional_float(latest["adx"]),
            "volume_ratio_5m": optional_float(latest["volume_ratio"]),
            "parameters": parameters.to_dict(),
        },
    )
