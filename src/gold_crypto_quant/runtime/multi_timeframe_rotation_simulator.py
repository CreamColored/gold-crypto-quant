"""按5m、15m、30m、1h优先级运行并由1分钟触轨执行的布林带影子模拟器。"""

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import pandas as pd

from gold_crypto_quant.runtime.bollinger_rotation_simulator import RotationPaperEvent
from gold_crypto_quant.strategy.bollinger_range import (
    build_rotation_box_context,
    parameters_for_same_timeframe,
)

MULTI_ROTATION_STRATEGY_VERSION = "5.7.0"
INTERVAL_PRIORITY = ("5m", "15m", "30m", "1h")
ENTRY_INTERVAL_PRIORITY = INTERVAL_PRIORITY
SYMBOL_PRIORITY = ("BTC_USDT", "ETH_USDT")
INTERVAL_DURATION = {
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
}
DEFAULT_MULTI_STATE_PATH = Path(".runtime/bollinger-multi-symbol-paper-v5.json")
LEGACY_MULTI_STATE_PATH = Path(".runtime/bollinger-multi-timeframe-paper-v4.json")
ETH_RULE_REFERENCE_PRICE = 2_500.0
PAPER_LEVERAGE = 125.0
FIXED_STOP_DISTANCE = {
    "BTC_USDT": {"5m": 250.0, "15m": 250.0, "30m": 500.0, "1h": 500.0},
    "ETH_USDT": {"5m": 5.0, "15m": 5.0, "30m": 10.0, "1h": 10.0},
}
OTHER_SYMBOL_STOP_RETURN = 0.40


@dataclass(slots=True)
class SymbolPaperPositionState:
    """单一品种跨周期共享的唯一仓位状态。"""

    position_side: str = ""
    active_interval: str = ""
    quantity: float = 0.0
    remaining_quantity: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    entry_fee_remaining: float = 0.0
    trade_net_pnl: float = 0.0
    middle_reduced: bool = False
    middle_reference_price: float = 0.0
    middle_trigger_price: float = 0.0
    # 开仓时命中反方向顶/底结构：多单查底部结构、空单查顶部结构，命中后到对侧轨不再止盈反手。
    structure_confirmed: bool = False
    # 到达对侧轨后若structure_confirmed为真，进入阶梯延续：每8点减仓一次，止损跟踪到上一触发价。
    trend_ride_active: bool = False
    trend_ride_next_trigger_price: float = 0.0
    # 该笔交易实际使用的阶梯步长；ETH为8点，BTC等高价品种按价格比例放大。
    trend_ride_step_points: float = 0.0
    middle_advance_distance: float = 0.0


@dataclass(slots=True)
class MultiTimeframePaperState:
    """各品种共享资金，但每个品种各自最多持有一笔仓位。"""

    strategy_version: str = MULTI_ROTATION_STRATEGY_VERSION
    equity: float = 10_000.0
    peak_equity: float = 10_000.0
    day_start_equity: float = 10_000.0
    current_day: str = ""
    last_bar_times: dict[str, dict[str, str]] = field(default_factory=dict)
    # 逐分钟触轨游标；同一根1分钟K线重复轮询时不能重复开仓或平仓。
    last_micro_bar_times: dict[str, str] = field(default_factory=dict)
    box_active: dict[str, dict[str, bool]] = field(default_factory=dict)
    blocked_after_stop: dict[str, dict[str, bool]] = field(default_factory=dict)
    reset_streak: dict[str, dict[str, int]] = field(default_factory=dict)
    positions: dict[str, SymbolPaperPositionState] = field(default_factory=dict)
    symbol_blocked_after_stop: dict[str, bool] = field(default_factory=dict)
    symbol_stopped_interval: dict[str, str] = field(default_factory=dict)
    symbol_resume_check_after: dict[str, str] = field(default_factory=dict)
    daily_blocked: bool = False
    permanent_fuse: bool = False


@dataclass(frozen=True, slots=True)
class MultiTimeframePaperSummary:
    """多周期影子模拟单轮结果。"""

    status: str
    processed_bars: int
    events: tuple[RotationPaperEvent, ...]
    equity: float
    position_side: str
    active_symbol: str
    active_interval: str
    selected_symbol: str
    selected_interval: str
    reason: str


@dataclass(frozen=True, slots=True)
class MicroEntryDecision:
    """主周期触轨时，1分钟和3分钟布林带对本次开仓的过滤结论。"""

    allowed: bool
    reason: str
    touch_time: pd.Timestamp | None = None
    percent_b_1m: float | None = None
    percent_b_3m: float | None = None


def _new_state(initial_equity: float, symbols: tuple[str, ...]) -> MultiTimeframePaperState:
    """创建所有品种、所有周期共享权益且全局单持仓的影子账户。"""
    return MultiTimeframePaperState(
        equity=initial_equity,
        peak_equity=initial_equity,
        day_start_equity=initial_equity,
        box_active={
            symbol: {interval: False for interval in INTERVAL_PRIORITY} for symbol in symbols
        },
        blocked_after_stop={
            symbol: {interval: False for interval in INTERVAL_PRIORITY} for symbol in symbols
        },
        reset_streak={
            symbol: {interval: 0 for interval in INTERVAL_PRIORITY} for symbol in symbols
        },
        positions={symbol: SymbolPaperPositionState() for symbol in symbols},
        symbol_blocked_after_stop={symbol: False for symbol in symbols},
        symbol_stopped_interval={symbol: "" for symbol in symbols},
        symbol_resume_check_after={symbol: "" for symbol in symbols},
        last_micro_bar_times={symbol: "" for symbol in symbols},
    )


def _load_state(path: Path) -> MultiTimeframePaperState | None:
    """读取V5多品种状态；不存在时由首次调用建立新账户。"""
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_positions = payload.pop("positions", {})
    stored_version = str(payload.get("strategy_version", ""))
    # 旧V5状态可原地升级：保留权益和已有仓位，仅补上新增的安全游标和等待字段。
    if stored_version in {"5.3.0", "5.4.0", "5.5.0", "5.6.0"}:
        payload["strategy_version"] = MULTI_ROTATION_STRATEGY_VERSION
        payload.setdefault(
            "symbol_blocked_after_stop",
            {symbol: False for symbol in raw_positions},
        )
        payload.setdefault(
            "symbol_stopped_interval",
            {symbol: "" for symbol in raw_positions},
        )
        payload.setdefault(
            "symbol_resume_check_after",
            {symbol: "" for symbol in raw_positions},
        )
        payload.setdefault(
            "last_micro_bar_times",
            {symbol: "" for symbol in raw_positions},
        )
    state = MultiTimeframePaperState(**payload)
    state.positions = {
        symbol: SymbolPaperPositionState(**position) for symbol, position in raw_positions.items()
    }
    if state.strategy_version != MULTI_ROTATION_STRATEGY_VERSION:
        raise RuntimeError("multi-timeframe paper state strategy version mismatch")
    return state


def _save_state(path: Path, state: MultiTimeframePaperState) -> None:
    """以原子替换保存影子账户，避免重启时读取半截JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _parameters_for_symbol(
    symbol: str,
    interval: str,
    bars: pd.DataFrame,
):
    """把以ETH点数定义的轨道和止损按BTC价格比例换算。"""
    parameters = parameters_for_same_timeframe(interval)
    if symbol in FIXED_STOP_DISTANCE:
        stop_distance = FIXED_STOP_DISTANCE[symbol][interval]
    else:
        # 其他币种按125倍杠杆下浮亏40%换算为价格反向波动0.32%。
        reference_price = float(pd.to_numeric(bars["close"].tail(20), errors="raise").median())
        stop_distance = reference_price * OTHER_SYMBOL_STOP_RETURN / PAPER_LEVERAGE
    if symbol != "BTC_USDT":
        return replace(parameters, fixed_stop_distance=stop_distance)
    # 调用最近20根收盘中位数，避免单根插针对换算比例产生明显影响。
    price_scale = max(
        1.0,
        float(pd.to_numeric(bars["close"].tail(20), errors="raise").median())
        / ETH_RULE_REFERENCE_PRICE,
    )
    return replace(
        parameters,
        fixed_stop_distance=stop_distance,
        maximum_band_drift=parameters.maximum_band_drift * price_scale,
        minimum_bandwidth=parameters.minimum_bandwidth * price_scale,
    )


def _block_symbol_after_stop(
    state: MultiTimeframePaperState,
    symbol: str,
    interval: str,
    stop_bar_open_time: pd.Timestamp,
) -> None:
    """一次止损作废全部旧箱体，并至少等待当前周期下一根K线完整收盘。"""
    state.symbol_blocked_after_stop[symbol] = True
    state.symbol_stopped_interval[symbol] = interval
    state.symbol_resume_check_after[symbol] = (
        stop_bar_open_time + INTERVAL_DURATION[interval] * 2
    ).isoformat()
    for blocked_interval in INTERVAL_PRIORITY:
        state.box_active[symbol][blocked_interval] = False
        state.blocked_after_stop[symbol][blocked_interval] = True
        state.reset_streak[symbol][blocked_interval] = 0


def _refresh_box_qualification(
    state: MultiTimeframePaperState,
    symbol: str,
    interval: str,
    *,
    box_candidate: bool,
    breakout: bool,
) -> None:
    """用最新已收盘K线刷新箱体资格；不允许沿用已经失效的旧箱体。"""
    if breakout or not box_candidate:
        state.box_active[symbol][interval] = False
        state.reset_streak[symbol][interval] = 0
        return
    if state.blocked_after_stop[symbol][interval]:
        state.box_active[symbol][interval] = False
        return
    state.box_active[symbol][interval] = True


def _bands_are_opening(context: pd.DataFrame, position: int) -> bool:
    """判断最新收盘后上下轨是否同时向外扩张，即布林带正在开口。"""
    if position < 1:
        return True
    previous = context.iloc[position - 1]
    current = context.iloc[position]
    return bool(
        float(current["bb_upper"]) > float(previous["bb_upper"])
        and float(current["bb_lower"]) < float(previous["bb_lower"])
        and float(current["bb_width"]) > float(previous["bb_width"])
    )


# 顶/底结构按MACD背离判定：价格创新高但DIF更低为顶背离，价格创新低但DIF更高为底背离。
STRUCTURE_MACD_FAST = 12
STRUCTURE_MACD_SLOW = 26
# 计算MACD需要足够长的历史，取150根让EMA26充分收敛。
STRUCTURE_MACD_HISTORY = 150
# 在最近40根已收盘K线里寻找两个摆动高/低点做背离比较。
STRUCTURE_SWING_WINDOW = 40
# 摆动点需左右各2根确认；右侧确认意味着最近2根不参与判定，这是必要的延迟而非未来数据。
STRUCTURE_SWING_CONFIRM = 2
STRUCTURE_LADDER_STEP_POINTS = 8.0


def _ladder_step_points(bars: pd.DataFrame) -> float:
    """阶梯止盈步长：ETH量级用8点原值，BTC等高价品种按价格中位数等比例放大。

    8点是按ETH价位定的绝对点数，直接套到BTC（约0.01%）只相当于噪音，
    因此复用与走平阈值相同的价格比例换算。
    """
    price_scale = max(
        1.0,
        float(pd.to_numeric(bars["close"].tail(20), errors="raise").median())
        / ETH_RULE_REFERENCE_PRICE,
    )
    return STRUCTURE_LADDER_STEP_POINTS * price_scale


def _closed_bars_before(
    bars: pd.DataFrame,
    duration: pd.Timedelta,
    as_of: pd.Timestamp,
    lookback: int,
) -> pd.DataFrame:
    """返回as_of之前已完整收盘的最近lookback根K线，避免读取未来数据。"""
    available = bars.index + duration <= as_of
    return bars.loc[available].tail(lookback)


def _macd_dif(bars: pd.DataFrame) -> pd.Series:
    """按标准MACD(12,26)计算DIF快线，用于顶底背离判定。"""
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    fast = close.ewm(span=STRUCTURE_MACD_FAST, adjust=False).mean()
    slow = close.ewm(span=STRUCTURE_MACD_SLOW, adjust=False).mean()
    return fast - slow


def _swing_positions(values, *, high: bool) -> list[int]:
    """返回左右各STRUCTURE_SWING_CONFIRM根都不更极端的摆动点下标。"""
    confirm = STRUCTURE_SWING_CONFIRM
    found: list[int] = []
    for index in range(confirm, len(values) - confirm):
        window = values[index - confirm : index + confirm + 1]
        pivot = values[index]
        if high and pivot >= window.max() and pivot > window[0]:
            found.append(index)
        elif not high and pivot <= window.min() and pivot < window[0]:
            found.append(index)
    return found


def _has_top_structure(bars: pd.DataFrame) -> bool:
    """顶部结构＝MACD顶背离：后一个摆动高点收盘价更高，但对应DIF更低。

    ``bars`` 需要传入截止判定时刻的完整已收盘序列，函数内部自行计算MACD并回看，
    因为DIF依赖长历史，先截短再算会得到失真的快线。
    """
    if len(bars) < STRUCTURE_MACD_SLOW + STRUCTURE_SWING_CONFIRM * 2:
        return False
    dif = _macd_dif(bars.tail(STRUCTURE_MACD_HISTORY))
    close = pd.to_numeric(bars["close"], errors="raise").astype(float).tail(len(dif))
    closes = close.tail(STRUCTURE_SWING_WINDOW).to_numpy()
    difs = dif.tail(STRUCTURE_SWING_WINDOW).to_numpy()
    pivots = _swing_positions(closes, high=True)
    if len(pivots) < 2:
        return False
    # 以窗口内价格最高的摆动点作为“新高”，再往前找一个价更低但动能更强的高点。
    peak = max(pivots, key=lambda index: closes[index])
    return any(
        closes[index] < closes[peak] and difs[index] > difs[peak]
        for index in pivots
        if index < peak
    )


def _has_bottom_structure(bars: pd.DataFrame) -> bool:
    """底部结构＝MACD底背离：后一个摆动低点收盘价更低，但对应DIF更高。"""
    if len(bars) < STRUCTURE_MACD_SLOW + STRUCTURE_SWING_CONFIRM * 2:
        return False
    dif = _macd_dif(bars.tail(STRUCTURE_MACD_HISTORY))
    close = pd.to_numeric(bars["close"], errors="raise").astype(float).tail(len(dif))
    closes = close.tail(STRUCTURE_SWING_WINDOW).to_numpy()
    difs = dif.tail(STRUCTURE_SWING_WINDOW).to_numpy()
    pivots = _swing_positions(closes, high=False)
    if len(pivots) < 2:
        return False
    # 以窗口内价格最低的摆动点作为“新低”，再往前找一个价更高但动能更弱的低点。
    trough = min(pivots, key=lambda index: closes[index])
    return any(
        closes[index] > closes[trough] and difs[index] < difs[trough]
        for index in pivots
        if index < trough
    )


def _resample_three_minute_bars(bars_1m: pd.DataFrame) -> pd.DataFrame:
    """把UTC自然分钟对齐的1分钟K线聚合为无未来数据的3分钟K线。"""
    return (
        bars_1m.resample("3min", origin="epoch", label="left", closed="left")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna()
    )


def _micro_bollinger_context(bars: pd.DataFrame) -> pd.DataFrame:
    """计算20周期、2倍标准差的小周期布林带。"""
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    middle = close.rolling(20).mean()
    deviation = close.rolling(20).std(ddof=0)
    return pd.DataFrame(
        {
            "bb_upper": middle + 2.0 * deviation,
            "bb_middle": middle,
            "bb_lower": middle - 2.0 * deviation,
        },
        index=bars.index,
    )


def _micro_zones_allow_entry(
    symbol: str,
    side: str,
    percent_b_1m: float,
    percent_b_3m: float,
) -> tuple[bool, str]:
    """应用一周复盘得到的小周期上下轨共振过滤规则。"""
    if side == "LONG":
        allowed = percent_b_1m <= 0.2 and percent_b_3m <= 0.2
        return allowed, (
            "1分钟和3分钟均位于下轨侧" if allowed else "做多时1分钟或3分钟未处于下轨侧"
        )
    aligned_upper = percent_b_1m >= 0.8 and percent_b_3m >= 0.8
    if not aligned_upper:
        return False, "做空时1分钟或3分钟处于中轨或非上轨区域"
    if symbol == "ETH_USDT" and percent_b_1m >= 1.0 and percent_b_3m >= 1.0:
        return False, "ETH的1分钟和3分钟同时位于上轨外，短线上冲时禁止做空"
    return True, "1分钟和3分钟均位于上轨侧"


def _evaluate_micro_entry(
    symbol: str,
    side: str,
    reference: float,
    bar_open_time: pd.Timestamp,
    interval: str,
    bars_1m: pd.DataFrame,
    context_1m: pd.DataFrame,
    context_3m: pd.DataFrame,
) -> MicroEntryDecision:
    """在主周期触轨分钟，用当时已收完的小周期K线过滤开仓。"""
    window_end = bar_open_time + INTERVAL_DURATION[interval]
    window = bars_1m[(bars_1m.index >= bar_open_time) & (bars_1m.index < window_end)]
    touched = window["low"].le(reference) if side == "LONG" else window["high"].ge(reference)
    if not touched.any():
        return MicroEntryDecision(False, "1分钟数据未确认主周期轨道触碰")
    touch_time = pd.Timestamp(touched[touched].index[0])
    completed_1m = context_1m[(context_1m.index + pd.Timedelta(minutes=1)) <= touch_time].dropna()
    completed_3m = context_3m[(context_3m.index + pd.Timedelta(minutes=3)) <= touch_time].dropna()
    if len(completed_1m) < 1 or len(completed_3m) < 1:
        return MicroEntryDecision(False, "小周期布林带预热不足", touch_time=touch_time)
    latest_1m = completed_1m.iloc[-1]
    latest_3m = completed_3m.iloc[-1]

    def percent_b(row: pd.Series) -> float | None:
        width = float(row["bb_upper"]) - float(row["bb_lower"])
        return (reference - float(row["bb_lower"])) / width if width > 0 else None

    percent_b_1m = percent_b(latest_1m)
    percent_b_3m = percent_b(latest_3m)
    if percent_b_1m is None or percent_b_3m is None:
        return MicroEntryDecision(False, "小周期布林带宽度为零", touch_time=touch_time)
    allowed, reason = _micro_zones_allow_entry(
        symbol,
        side,
        percent_b_1m,
        percent_b_3m,
    )
    return MicroEntryDecision(
        allowed,
        reason,
        touch_time=touch_time,
        percent_b_1m=percent_b_1m,
        percent_b_3m=percent_b_3m,
    )


def _evaluate_micro_entry_at_touch(
    symbol: str,
    side: str,
    reference: float,
    touch_time: pd.Timestamp,
    context_1m: pd.DataFrame,
    context_3m: pd.DataFrame,
) -> MicroEntryDecision:
    """使用触轨分钟开始前已经收完的1m/3m布林带过滤即时开仓。

    当前1分钟K线的最高价或最低价负责证明“已经触轨”，但过滤指标只能读取
    touch_time以前完成的K线，避免把触轨后的价格变化反过来用于批准本次开仓。
    """
    completed_1m = context_1m[(context_1m.index + pd.Timedelta(minutes=1)) <= touch_time].dropna()
    completed_3m = context_3m[(context_3m.index + pd.Timedelta(minutes=3)) <= touch_time].dropna()
    if completed_1m.empty or completed_3m.empty:
        return MicroEntryDecision(False, "小周期布林带预热不足", touch_time=touch_time)

    def percent_b(row: pd.Series) -> float | None:
        width = float(row["bb_upper"]) - float(row["bb_lower"])
        return (reference - float(row["bb_lower"])) / width if width > 0 else None

    percent_b_1m = percent_b(completed_1m.iloc[-1])
    percent_b_3m = percent_b(completed_3m.iloc[-1])
    if percent_b_1m is None or percent_b_3m is None:
        return MicroEntryDecision(False, "小周期布林带宽度为零", touch_time=touch_time)
    allowed, reason = _micro_zones_allow_entry(
        symbol,
        side,
        percent_b_1m,
        percent_b_3m,
    )
    return MicroEntryDecision(
        allowed,
        reason,
        touch_time=touch_time,
        percent_b_1m=percent_b_1m,
        percent_b_3m=percent_b_3m,
    )


def _try_release_symbol_after_wait(
    state: MultiTimeframePaperState,
    symbol: str,
    close_time: pd.Timestamp,
    batch: list[tuple[str, str, int]],
    contexts: dict[str, dict[str, pd.DataFrame]],
) -> bool:
    """等待对应周期收线后检查新箱体；开口或非震荡时继续封锁。"""
    if not state.symbol_blocked_after_stop.get(symbol, False):
        return False
    interval = state.symbol_stopped_interval.get(symbol, "")
    check_after = state.symbol_resume_check_after.get(symbol, "")
    if not interval or not check_after or close_time < pd.Timestamp(check_after):
        return False
    active_item = next(
        (item for item in batch if item[0] == symbol and item[1] == interval),
        None,
    )
    if active_item is None:
        return False
    position = active_item[2]
    current = contexts[symbol][interval].iloc[position]
    valid_box = bool(current["box_candidate"]) and not bool(current["breakout"])
    if not valid_box or _bands_are_opening(contexts[symbol][interval], position):
        return False
    state.symbol_blocked_after_stop[symbol] = False
    state.symbol_stopped_interval[symbol] = ""
    state.symbol_resume_check_after[symbol] = ""
    for candidate_interval in INTERVAL_PRIORITY:
        state.blocked_after_stop[symbol][candidate_interval] = False
        state.box_active[symbol][candidate_interval] = False
        state.reset_streak[symbol][candidate_interval] = 0
    state.box_active[symbol][interval] = True
    return True


def _middle_reduction_trigger(
    symbol: str,
    side: str,
    entry_price: float,
    middle_price: float,
    point_scale: float,
) -> tuple[float, float]:
    """按ETH点数规则或其他币的杠杆收益规则返回减仓触发价。"""
    distance = abs(entry_price - middle_price)
    if symbol == "ETH_USDT":
        advance = 2.0 * point_scale if distance >= 10.0 * point_scale else 0.0
        trigger = middle_price - advance if side == "LONG" else middle_price + advance
        return trigger, advance
    projected_leveraged_return = distance / entry_price * PAPER_LEVERAGE
    if projected_leveraged_return + 1e-12 < 1.0:
        return middle_price, 0.0
    # 到中轨预计杠杆收益达到100%时，走完价格路径的80%提前减半仓。
    trigger = entry_price + (middle_price - entry_price) * 0.8
    advance = abs(middle_price - trigger)
    return trigger, advance


def run_multi_timeframe_paper_cycle(
    bars_by_interval: (dict[str, pd.DataFrame] | dict[str, dict[str, pd.DataFrame]]),
    *,
    micro_bars_by_symbol: dict[str, pd.DataFrame] | None = None,
    state_path: Path = DEFAULT_MULTI_STATE_PATH,
    initial_equity: float = 10_000.0,
    maker_fee_rate: float = -0.0001,
    taker_fee_rate: float = 0.00075,
    stop_slippage_rate: float = 0.0002,
    risk_per_trade: float = 0.0025,
) -> MultiTimeframePaperSummary:
    """按周期、品种优先级逐批处理K线，全系统始终最多持有一笔仓位。"""
    # 保留旧的单品种测试调用形式；正式运行传入“品种→周期→K线”的两层结构。
    first_value = next(iter(bars_by_interval.values()), None)
    if isinstance(first_value, pd.DataFrame):
        bars_by_symbol = {"ETH_USDT": bars_by_interval}
    else:
        bars_by_symbol = bars_by_interval
    symbols = tuple(symbol for symbol in SYMBOL_PRIORITY if symbol in bars_by_symbol) + tuple(
        sorted(set(bars_by_symbol).difference(SYMBOL_PRIORITY))
    )
    if not symbols:
        raise ValueError("multi-symbol simulator requires at least one symbol")
    for symbol, symbol_bars in bars_by_symbol.items():
        if set(symbol_bars) != set(INTERVAL_PRIORITY):
            raise ValueError(f"{symbol} requires 5m, 15m, 30m and 1h market bars")
        for interval, bars in symbol_bars.items():
            if len(bars) < 30:
                raise ValueError(f"{symbol} {interval} requires at least 30 bars")
    parameters_by_market = {
        symbol: {
            interval: _parameters_for_symbol(symbol, interval, bars)
            for interval, bars in symbol_bars.items()
        }
        for symbol, symbol_bars in bars_by_symbol.items()
    }
    contexts = {
        symbol: {
            interval: build_rotation_box_context(bars, parameters_by_market[symbol][interval])
            for interval, bars in symbol_bars.items()
        }
        for symbol, symbol_bars in bars_by_symbol.items()
    }
    micro_contexts: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    micro_three_minute_bars: dict[str, pd.DataFrame] = {}
    if micro_bars_by_symbol is not None:
        missing_micro = set(symbols).difference(micro_bars_by_symbol)
        if missing_micro:
            raise ValueError(f"missing 1m bars for symbols: {sorted(missing_micro)}")
        for symbol in symbols:
            bars_1m = micro_bars_by_symbol[symbol]
            if len(bars_1m) < 65:
                raise ValueError(f"{symbol} requires at least 65 one-minute bars")
            bars_3m = _resample_three_minute_bars(bars_1m)
            micro_three_minute_bars[symbol] = bars_3m
            micro_contexts[symbol] = (
                bars_1m,
                _micro_bollinger_context(bars_1m),
                _micro_bollinger_context(bars_3m),
            )
    state = _load_state(state_path)
    if state is None:
        # 默认正式状态首次升级时继承已结束持仓的V4权益，但绝不搬运活动仓位。
        legacy: dict[str, object] | None = None
        if state_path == DEFAULT_MULTI_STATE_PATH and LEGACY_MULTI_STATE_PATH.exists():
            legacy = json.loads(LEGACY_MULTI_STATE_PATH.read_text(encoding="utf-8"))
            if legacy.get("position_side"):
                raise RuntimeError("cannot migrate V4 while a shadow position is open")
            initial_equity = float(legacy.get("equity", initial_equity))
        state = _new_state(initial_equity, symbols)
        if legacy is not None:
            # 调用旧状态的权益峰值和当日起点，升级不能把既有回撤或日亏损清零。
            state.peak_equity = max(state.equity, float(legacy.get("peak_equity", state.equity)))
            state.day_start_equity = float(legacy.get("day_start_equity", state.equity))
            state.current_day = str(legacy.get("current_day", ""))
            state.daily_blocked = bool(legacy.get("daily_blocked", False))
            state.permanent_fuse = bool(legacy.get("permanent_fuse", False))
        state.last_bar_times = {
            symbol: {
                interval: bars.index[-1].isoformat()
                for interval, bars in bars_by_symbol[symbol].items()
            }
            for symbol in symbols
        }
        if micro_bars_by_symbol is not None:
            # 首次启用逐分钟触轨时从最新1分钟K线开始，禁止把历史触轨集中重放成新交易。
            state.last_micro_bar_times = {
                symbol: micro_bars_by_symbol[symbol].index[-1].isoformat()
                for symbol in symbols
            }
        # 新策略首次启动只建立两个品种、三个交易周期游标，禁止回放历史成交。
        _save_state(state_path, state)
        return MultiTimeframePaperSummary(
            status="INITIALIZED",
            processed_bars=0,
            events=(),
            equity=state.equity,
            position_side="",
            active_symbol="",
            active_interval="",
            selected_symbol="",
            selected_interval="",
            reason="V5共享影子账户已从各品种各周期最新收盘K线开始",
        )

    # 老状态文件里没见过的新品种（例如新接入的XAU_USDT）在此原地补齐游标和仓位槽位，
    # 从当前最新已收盘K线开始观察，不回放该品种接入前的历史箱体或触轨。
    for symbol in symbols:
        if symbol in state.positions:
            continue
        state.positions[symbol] = SymbolPaperPositionState()
        state.symbol_blocked_after_stop[symbol] = False
        state.symbol_stopped_interval[symbol] = ""
        state.symbol_resume_check_after[symbol] = ""
        state.box_active[symbol] = {interval: False for interval in INTERVAL_PRIORITY}
        state.blocked_after_stop[symbol] = {interval: False for interval in INTERVAL_PRIORITY}
        state.reset_streak[symbol] = {interval: 0 for interval in INTERVAL_PRIORITY}
        state.last_bar_times[symbol] = {
            interval: bars.index[-1].isoformat()
            for interval, bars in bars_by_symbol[symbol].items()
        }

    # V5.6原地升级到V5.7时仅建立逐分钟游标，保留当前仓位和全部权益。
    # 本轮不追溯执行升级前已经结束的1分钟触轨，下一轮开始即时观察。
    micro_cursor_initialized = False
    if micro_bars_by_symbol is not None:
        for symbol in symbols:
            if not state.last_micro_bar_times.get(symbol):
                state.last_micro_bar_times[symbol] = (
                    micro_bars_by_symbol[symbol].index[-1].isoformat()
                )
                micro_cursor_initialized = True

    events: list[RotationPaperEvent] = []

    def micro_decision(
        symbol: str,
        side: str,
        reference: float,
        bar_open_time: pd.Timestamp,
        interval: str,
    ) -> MicroEntryDecision:
        """未传微周期数据时保持测试兼容；正式运行和新回测必须传入1分钟。"""
        if symbol not in micro_contexts:
            return MicroEntryDecision(True, "未启用小周期过滤")
        bars_1m, context_1m, context_3m = micro_contexts[symbol]
        return _evaluate_micro_entry(
            symbol,
            side,
            reference,
            bar_open_time,
            interval,
            bars_1m,
            context_1m,
            context_3m,
        )

    def add_event(
        timestamp: pd.Timestamp,
        title: str,
        symbol: str,
        interval: str,
        *lines: str,
        severity: str = "INFO",
    ) -> None:
        """生成包含策略周期的唯一邮件事件。"""
        events.append(
            RotationPaperEvent(
                event_key=(
                    f"rotation-v5:{timestamp.isoformat()}:{symbol}:{interval}:{title}:{len(events)}"
                ),
                title=title,
                lines=(
                    f"北京时间：{timestamp.tz_convert('Asia/Shanghai')}",
                    f"品种：{symbol}",
                    f"交易周期：{interval}",
                    *lines,
                ),
                severity=severity,
            )
        )

    def open_position(
        side: str,
        symbol: str,
        interval: str,
        reference: float,
        middle_reference: float,
        timestamp: pd.Timestamp,
        reason: str,
    ) -> None:
        """按全账户0.25%风险建立该品种唯一仓位。"""
        parameters = parameters_by_market[symbol][interval]
        position = state.positions[symbol]
        position.position_side = side
        position.active_interval = interval
        position.entry_price = reference
        position.stop_price = (
            reference - parameters.fixed_stop_distance
            if side == "LONG"
            else reference + parameters.fixed_stop_distance
        )
        stop_fill = position.stop_price * (
            1.0 - stop_slippage_rate if side == "LONG" else 1.0 + stop_slippage_rate
        )
        adverse_loss = reference - stop_fill if side == "LONG" else stop_fill - reference
        loss_per_unit = adverse_loss + reference * maker_fee_rate + stop_fill * taker_fee_rate
        position.quantity = state.equity * risk_per_trade / loss_per_unit
        position.remaining_quantity = position.quantity
        position.entry_fee_remaining = reference * position.quantity * maker_fee_rate
        state.equity -= position.entry_fee_remaining
        position.trade_net_pnl = 0.0
        position.middle_reduced = False
        position.structure_confirmed = has_reversal_structure(symbol, interval, side, timestamp)
        position.trend_ride_active = False
        position.trend_ride_next_trigger_price = 0.0
        position.trend_ride_step_points = 0.0
        # ETH的“10点距离、提前2点”是固定价格点数，不能随各周期止损距离放大。
        point_scale = 1.0
        middle_trigger, middle_advance = _middle_reduction_trigger(
            symbol,
            side,
            reference,
            middle_reference,
            point_scale,
        )
        position.middle_reference_price = middle_reference
        position.middle_trigger_price = middle_trigger
        position.middle_advance_distance = middle_advance
        if symbol == "ETH_USDT":
            reduction_rule = (
                f"ETH距离中轨≥10点，提前{middle_advance:.2f}点"
                if middle_advance > 0
                else "ETH距离中轨<10点，触碰中轨"
            )
        else:
            projected_return = abs(reference - middle_reference) / reference * PAPER_LEVERAGE
            reduction_rule = (
                f"预计到中轨杠杆收益{projected_return:.2%}，80%路径减仓"
                if middle_advance > 0
                else f"预计到中轨杠杆收益{projected_return:.2%}，不足100%等中轨"
            )
        add_event(
            timestamp,
            f"模拟开仓：{'买入做多' if side == 'LONG' else '卖出做空'}",
            symbol,
            interval,
            f"原因：{reason}",
            f"轨道限价：{reference:.2f}",
            f"数量：{position.quantity:.6f} {symbol.split('_', 1)[0]}",
            f"保护止损：{position.stop_price:.2f}",
            f"中轨参考价：{middle_reference:.2f}",
            f"减仓触发价：{middle_trigger:.2f}（{reduction_rule}）",
            (
                f"{'底部' if side == 'LONG' else '顶部'}结构："
                + (
                    "已确认，对侧轨不止盈反手，改为阶梯延续"
                    if position.structure_confirmed
                    else "未确认"
                )
            ),
            f"影子权益：{state.equity:.2f} USDT",
        )

    def close_quantity(
        symbol: str,
        quantity: float,
        reference: float,
        timestamp: pd.Timestamp,
        reason: str,
        *,
        market: bool,
    ) -> None:
        """模拟指定品种的减仓或平仓，并把成本和盈亏计入共享权益。"""
        position = state.positions[symbol]
        interval = position.active_interval
        if market:
            fill = reference * (
                1.0 - stop_slippage_rate
                if position.position_side == "LONG"
                else 1.0 + stop_slippage_rate
            )
            fee_rate = taker_fee_rate
        else:
            fill = reference
            fee_rate = maker_fee_rate
        gross = (
            (fill - position.entry_price) * quantity
            if position.position_side == "LONG"
            else (position.entry_price - fill) * quantity
        )
        allocated_entry_fee = position.entry_fee_remaining * (
            quantity / position.remaining_quantity
        )
        exit_fee = fill * quantity * fee_rate
        net = gross - allocated_entry_fee - exit_fee
        state.equity += gross - exit_fee
        position.trade_net_pnl += net
        position.entry_fee_remaining -= allocated_entry_fee
        position.remaining_quantity -= quantity
        add_event(
            timestamp,
            f"模拟{'减仓' if position.remaining_quantity > 1e-12 else '平仓'}：{reason}",
            symbol,
            interval,
            f"方向：{position.position_side}",
            f"成交参考价：{fill:.2f}",
            f"本次净盈亏：{net:+.2f} USDT",
            f"整笔累计净盈亏：{position.trade_net_pnl:+.2f} USDT",
            f"影子权益：{state.equity:.2f} USDT",
            severity="WARNING" if net < 0 else "INFO",
        )

    def confirmed_context_at(
        symbol: str,
        interval: str,
        minute_open_time: pd.Timestamp,
    ) -> pd.Series | None:
        """返回该分钟开始前已经确认的主周期布林带，绝不读取未收线高周期。"""
        context = contexts[symbol][interval]
        available = context.index + INTERVAL_DURATION[interval] <= minute_open_time
        if not available.any():
            return None
        return context.loc[available].iloc[-1]

    def has_reversal_structure(
        symbol: str,
        interval: str,
        new_side: str,
        as_of: pd.Timestamp,
    ) -> bool:
        """反手方向若命中对应结构则保留原方向：多单查底部结构、空单查顶部结构；
        当前主周期或1分钟、3分钟任一确认即成立。"""
        checker = _has_bottom_structure if new_side == "LONG" else _has_top_structure
        # MACD依赖长历史，这里传入截止判定时刻的完整已收盘序列，由判定函数内部回看。
        main_bars = _closed_bars_before(
            bars_by_symbol[symbol][interval],
            INTERVAL_DURATION[interval],
            as_of,
            STRUCTURE_MACD_HISTORY,
        )
        if checker(main_bars):
            return True
        if symbol not in micro_contexts:
            return False
        bars_1m = micro_contexts[symbol][0]
        if checker(
            _closed_bars_before(bars_1m, pd.Timedelta(minutes=1), as_of, STRUCTURE_MACD_HISTORY)
        ):
            return True
        bars_3m = micro_three_minute_bars.get(symbol)
        if bars_3m is not None and checker(
            _closed_bars_before(bars_3m, pd.Timedelta(minutes=3), as_of, STRUCTURE_MACD_HISTORY)
        ):
            return True
        return False

    def stop_interval_open_time(timestamp: pd.Timestamp, interval: str) -> pd.Timestamp:
        """把逐分钟止损时间归属到对应主周期K线，供完整收线等待规则使用。"""
        frequency = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}[interval]
        return timestamp.floor(frequency)

    # V5.7把已确认箱体后的触轨、止损、减仓和止盈全部下沉到1分钟。
    # 行情服务每分钟调用一次，因此不必等待15m/30m/1h收线才发送交易事件。
    if micro_contexts and not micro_cursor_initialized:
        minute_items: list[tuple[pd.Timestamp, str, int]] = []
        for symbol in symbols:
            bars_1m = micro_contexts[symbol][0]
            last_micro_time = pd.Timestamp(state.last_micro_bar_times[symbol])
            for position, minute_open_time in enumerate(bars_1m.index):
                if minute_open_time > last_micro_time:
                    minute_items.append((pd.Timestamp(minute_open_time), symbol, position))

        for minute_open_time, symbol, minute_position in sorted(
            minute_items,
            key=lambda item: (item[0], symbols.index(item[1])),
        ):
            bars_1m = micro_contexts[symbol][0]
            minute_bar = bars_1m.iloc[minute_position]
            position = state.positions[symbol]
            traded_this_minute = False

            # 已持仓时先执行保护止损，再判断对侧轨、减仓；风险动作永远优先于新开仓。
            if position.position_side:
                interval = position.active_interval
                main = confirmed_context_at(symbol, interval, minute_open_time)
                if main is not None:
                    upper = float(main["bb_upper"])
                    lower = float(main["bb_lower"])
                    stop_hit = (
                        position.position_side == "LONG"
                        and float(minute_bar["low"]) <= position.stop_price
                    ) or (
                        position.position_side == "SHORT"
                        and float(minute_bar["high"]) >= position.stop_price
                    )
                    target_hit = (
                        position.position_side == "LONG" and float(minute_bar["high"]) >= upper
                    ) or (
                        position.position_side == "SHORT" and float(minute_bar["low"]) <= lower
                    )
                    if stop_hit:
                        reason = (
                            "中轨减仓后的保本止损"
                            if position.middle_reduced and not position.trend_ride_active
                            else "阶梯延续止损"
                            if position.trend_ride_active
                            else "固定保护止损"
                        )
                        close_quantity(
                            symbol,
                            position.remaining_quantity,
                            position.stop_price,
                            minute_open_time,
                            reason,
                            market=True,
                        )
                        position.position_side = ""
                        position.active_interval = ""
                        _block_symbol_after_stop(
                            state,
                            symbol,
                            interval,
                            stop_interval_open_time(minute_open_time, interval),
                        )
                        traded_this_minute = True
                    elif position.trend_ride_active:
                        # 结构延续阶段：只看下一个阶梯触发价，不再判断对侧轨或中轨。
                        trigger = position.trend_ride_next_trigger_price
                        # 旧状态文件没有步长字段时回算一次，避免步长为0导致阶梯原地踏步。
                        step = position.trend_ride_step_points or _ladder_step_points(
                            bars_by_symbol[symbol][interval]
                        )
                        ladder_hit = (
                            position.position_side == "LONG"
                            and float(minute_bar["high"]) >= trigger
                        ) or (
                            position.position_side == "SHORT"
                            and float(minute_bar["low"]) <= trigger
                        )
                        if ladder_hit:
                            close_quantity(
                                symbol,
                                position.remaining_quantity * 0.5,
                                trigger,
                                minute_open_time,
                                f"结构延续：每{step:.2f}点阶梯减仓50%",
                                market=False,
                            )
                            going_long = position.position_side == "LONG"
                            # 止损滞后一档，落在上一个减仓价上，与当前价保持一个步长的缓冲。
                            position.stop_price = (
                                trigger - step if going_long else trigger + step
                            )
                            position.trend_ride_next_trigger_price = (
                                trigger + step if going_long else trigger - step
                            )
                            traded_this_minute = True
                    elif target_hit:
                        old_side = position.position_side
                        target = upper if old_side == "LONG" else lower
                        if position.structure_confirmed:
                            # 对侧轨命中反方向结构：不止盈反手，改为减仓50%、止损收紧到中轨，
                            # 只往结构确认的方向继续看，进入阶梯延续。
                            step = _ladder_step_points(bars_by_symbol[symbol][interval])
                            close_quantity(
                                symbol,
                                position.remaining_quantity * 0.5,
                                target,
                                minute_open_time,
                                f"对侧轨结构确认：减仓50%延续原方向，不反手（步长{step:.2f}点）",
                                market=False,
                            )
                            position.stop_price = float(main["bb_middle"])
                            position.trend_ride_active = True
                            position.trend_ride_step_points = step
                            position.trend_ride_next_trigger_price = (
                                target - step if old_side == "SHORT" else target + step
                            )
                            traded_this_minute = True
                        else:
                            close_quantity(
                                symbol,
                                position.remaining_quantity,
                                target,
                                minute_open_time,
                                "到达对侧轨止盈",
                                market=False,
                            )
                            position.position_side = ""
                            position.active_interval = ""
                            traded_this_minute = True
                            # 箱体仍有效时，触及对侧轨即可原价反手；不等待主周期收线。
                            box_valid = (
                                bool(main["box_candidate"])
                                and not bool(main["breakout"])
                                and state.box_active[symbol][interval]
                                and not state.blocked_after_stop[symbol][interval]
                                and interval in ENTRY_INTERVAL_PRIORITY
                                and not state.daily_blocked
                                and not state.permanent_fuse
                            )
                            if box_valid:
                                new_side = "SHORT" if old_side == "LONG" else "LONG"
                                open_position(
                                    new_side,
                                    symbol,
                                    interval,
                                    target,
                                    float(main["bb_middle"]),
                                    minute_open_time,
                                    "对侧轨止盈后触轨即时反手",
                                )
                    else:
                        middle_trigger = position.middle_trigger_price or float(main["bb_middle"])
                        middle_hit = (
                            position.position_side == "LONG"
                            and float(minute_bar["high"]) >= middle_trigger
                        ) or (
                            position.position_side == "SHORT"
                            and float(minute_bar["low"]) <= middle_trigger
                        )
                        if middle_hit and not position.middle_reduced:
                            middle_reason = (
                                f"距离中轨{position.middle_advance_distance:.2f}点提前减仓50%"
                                if position.middle_advance_distance > 0
                                else "到达中轨减仓50%"
                            )
                            close_quantity(
                                symbol,
                                position.remaining_quantity * 0.5,
                                middle_trigger,
                                minute_open_time,
                                middle_reason,
                                market=False,
                            )
                            position.middle_reduced = True
                            position.stop_price = position.entry_price
                            traded_this_minute = True

            # 空仓时按5m→15m→30m→1h选择已经确认的最高优先级箱体，一触轨即开仓。
            if (
                not position.position_side
                and not traded_this_minute
                and not state.symbol_blocked_after_stop.get(symbol, False)
                and not state.daily_blocked
                and not state.permanent_fuse
            ):
                selected: tuple[str, pd.Series] | None = None
                for interval in ENTRY_INTERVAL_PRIORITY:
                    main = confirmed_context_at(symbol, interval, minute_open_time)
                    if (
                        main is not None
                        and state.box_active[symbol][interval]
                        and not state.blocked_after_stop[symbol][interval]
                        and bool(main["box_candidate"])
                        and not bool(main["breakout"])
                    ):
                        selected = (interval, main)
                        break
                if selected is not None:
                    interval, main = selected
                    upper = float(main["bb_upper"])
                    lower = float(main["bb_lower"])
                    touched_upper = float(minute_bar["high"]) >= upper
                    touched_lower = float(minute_bar["low"]) <= lower
                    # 同一分钟同时穿过上下轨时无法还原先后顺序，保守跳过而不猜测方向。
                    if touched_upper != touched_lower:
                        side = "SHORT" if touched_upper else "LONG"
                        reference = upper if touched_upper else lower
                        open_position(
                            side,
                            symbol,
                            interval,
                            reference,
                            float(main["bb_middle"]),
                            minute_open_time,
                            f"{interval}震荡箱体触及{'上轨' if touched_upper else '下轨'}即时开仓",
                        )
                        immediate_stop = (
                            side == "LONG"
                            and float(minute_bar["low"]) <= position.stop_price
                        ) or (
                            side == "SHORT"
                            and float(minute_bar["high"]) >= position.stop_price
                        )
                        if immediate_stop:
                            close_quantity(
                                symbol,
                                position.remaining_quantity,
                                position.stop_price,
                                minute_open_time,
                                "触轨开仓后同一分钟止损",
                                market=True,
                            )
                            position.position_side = ""
                            position.active_interval = ""
                            _block_symbol_after_stop(
                                state,
                                symbol,
                                interval,
                                stop_interval_open_time(minute_open_time, interval),
                            )
            state.last_micro_bar_times[symbol] = minute_open_time.isoformat()

    # 把两个品种、三个交易周期按实际收盘时刻合并，同一时刻先周期再品种排序。
    grouped: dict[pd.Timestamp, list[tuple[str, str, int]]] = {}
    for symbol in symbols:
        for interval in INTERVAL_PRIORITY:
            bars = bars_by_symbol[symbol][interval]
            last_time = pd.Timestamp(state.last_bar_times[symbol][interval])
            if last_time < bars.index[0] - INTERVAL_DURATION[interval]:
                raise RuntimeError(f"{symbol} {interval} paper history gap exceeds loaded window")
            for position, open_time in enumerate(bars.index):
                if open_time <= last_time:
                    continue
                close_time = pd.Timestamp(open_time) + INTERVAL_DURATION[interval]
                grouped.setdefault(close_time, []).append((symbol, interval, position))

    processed_bars = 0
    for close_time in sorted(grouped):
        batch = sorted(
            grouped[close_time],
            key=lambda item: (INTERVAL_PRIORITY.index(item[1]), symbols.index(item[0])),
        )
        batch_traded_symbols: set[str] = set()
        beijing_day = str(close_time.tz_convert("Asia/Shanghai").date())
        if state.current_day != beijing_day:
            state.current_day = beijing_day
            state.day_start_equity = state.equity
            state.daily_blocked = False

        # 每根已收盘K线都重新确认箱体资格；条件失效时立即清除旧箱体。
        for symbol, interval, position in batch:
            if position < 1:
                continue
            previous = contexts[symbol][interval].iloc[position - 1]
            _refresh_box_qualification(
                state,
                symbol,
                interval,
                box_candidate=bool(previous["box_candidate"]),
                breakout=bool(previous["breakout"]),
            )

        # 止损后必须等对应周期的下一根K线完整收盘，再用该根的新布林带判断。
        # 本批刚解除封锁也不能追溯使用已经走完的K线开仓，只能从下一根开始。
        rearmed_symbols = {
            symbol
            for symbol in symbols
            if _try_release_symbol_after_wait(
                state,
                symbol,
                close_time,
                batch,
                contexts,
            )
        }

        # 每个品种各自管理唯一仓位；一个品种的动作不阻止另一个品种独立交易。
        for symbol in symbols:
            # 正式运行传入1分钟行情时，V5.7已经在上面的逐分钟循环完成全部仓位动作。
            # 主周期代码仅保留给不传1分钟数据的历史单元测试和兼容调用。
            if micro_contexts:
                continue
            symbol_position = state.positions[symbol]
            active_item = next(
                (
                    item
                    for item in batch
                    if item[0] == symbol and item[1] == symbol_position.active_interval
                ),
                None,
            )
            if not symbol_position.position_side or active_item is None:
                continue
            _, interval, position = active_item
            bars = bars_by_symbol[symbol][interval]
            bar = bars.iloc[position]
            previous = contexts[symbol][interval].iloc[position - 1]
            current = contexts[symbol][interval].iloc[position]
            upper = float(previous["bb_upper"])
            middle = float(previous["bb_middle"])
            lower = float(previous["bb_lower"])
            stop_hit = (
                symbol_position.position_side == "LONG"
                and float(bar["low"]) <= symbol_position.stop_price
            ) or (
                symbol_position.position_side == "SHORT"
                and float(bar["high"]) >= symbol_position.stop_price
            )
            target_hit = (
                symbol_position.position_side == "LONG" and float(bar["high"]) >= upper
            ) or (symbol_position.position_side == "SHORT" and float(bar["low"]) <= lower)
            if stop_hit:
                stop_reason = (
                    "中轨减仓后的保本止损" if symbol_position.middle_reduced else "固定保护止损"
                )
                close_quantity(
                    symbol,
                    symbol_position.remaining_quantity,
                    symbol_position.stop_price,
                    pd.Timestamp(bars.index[position]),
                    stop_reason,
                    market=True,
                )
                batch_traded_symbols.add(symbol)
                symbol_position.position_side = ""
                symbol_position.active_interval = ""
                _block_symbol_after_stop(
                    state,
                    symbol,
                    interval,
                    pd.Timestamp(bars.index[position]),
                )
                continue

            middle_trigger = symbol_position.middle_trigger_price or middle
            middle_hit = (
                symbol_position.position_side == "LONG" and float(bar["high"]) >= middle_trigger
            ) or (symbol_position.position_side == "SHORT" and float(bar["low"]) <= middle_trigger)
            if middle_hit and not symbol_position.middle_reduced and not target_hit:
                middle_reason = (
                    f"距离中轨{symbol_position.middle_advance_distance:.2f}点提前减仓50%"
                    if symbol_position.middle_advance_distance > 0
                    else "到达中轨减仓50%"
                )
                close_quantity(
                    symbol,
                    symbol_position.remaining_quantity * 0.5,
                    middle_trigger,
                    pd.Timestamp(bars.index[position]),
                    middle_reason,
                    market=False,
                )
                batch_traded_symbols.add(symbol)
                symbol_position.middle_reduced = True
                symbol_position.stop_price = symbol_position.entry_price
            if target_hit and symbol_position.position_side:
                old_side = symbol_position.position_side
                target = upper if old_side == "LONG" else lower
                close_quantity(
                    symbol,
                    symbol_position.remaining_quantity,
                    target,
                    pd.Timestamp(bars.index[position]),
                    "到达对侧轨止盈",
                    market=False,
                )
                batch_traded_symbols.add(symbol)
                symbol_position.position_side = ""
                current_box_valid = bool(current["box_candidate"]) and not bool(current["breakout"])
                if (
                    current_box_valid
                    and interval in ENTRY_INTERVAL_PRIORITY
                    and state.box_active[symbol][interval]
                    and not state.blocked_after_stop[symbol][interval]
                    and not state.daily_blocked
                    and not state.permanent_fuse
                ):
                    new_side = "SHORT" if old_side == "LONG" else "LONG"
                    entry_filter = micro_decision(
                        symbol,
                        new_side,
                        target,
                        pd.Timestamp(bars.index[position]),
                        interval,
                    )
                    if not entry_filter.allowed:
                        symbol_position.active_interval = ""
                        continue
                    open_position(
                        new_side,
                        symbol,
                        interval,
                        target,
                        middle,
                        pd.Timestamp(bars.index[position]),
                        f"对侧轨止盈后同周期反手；{entry_filter.reason}",
                    )
                    immediate_stop = (
                        new_side == "LONG" and float(bar["low"]) <= symbol_position.stop_price
                    ) or (new_side == "SHORT" and float(bar["high"]) >= symbol_position.stop_price)
                    if immediate_stop:
                        close_quantity(
                            symbol,
                            symbol_position.remaining_quantity,
                            symbol_position.stop_price,
                            pd.Timestamp(bars.index[position]),
                            "反手后同根K线止损",
                            market=True,
                        )
                        symbol_position.position_side = ""
                        symbol_position.active_interval = ""
                        _block_symbol_after_stop(
                            state,
                            symbol,
                            interval,
                            pd.Timestamp(bars.index[position]),
                        )
                else:
                    symbol_position.active_interval = ""

        # 每个空仓品种分别按5m→15m→30m→1h选择，品种之间允许同时开仓。
        for symbol in symbols:
            # 有1分钟行情时禁止在主周期收线后补做已经发生过的触轨交易。
            if micro_contexts:
                continue
            symbol_position = state.positions[symbol]
            if (
                symbol_position.position_side
                or symbol in batch_traded_symbols
                or symbol in rearmed_symbols
                or state.symbol_blocked_after_stop.get(symbol, False)
                or state.daily_blocked
                or state.permanent_fuse
            ):
                continue
            selected_item = next(
                (
                    (interval, position)
                    for item_symbol, interval, position in batch
                    if item_symbol == symbol
                    and interval in ENTRY_INTERVAL_PRIORITY
                    and position >= 1
                    and state.box_active[symbol][interval]
                    and not state.blocked_after_stop[symbol][interval]
                ),
                None,
            )
            if selected_item is None:
                continue
            interval, position = selected_item
            bars = bars_by_symbol[symbol][interval]
            bar = bars.iloc[position]
            previous = contexts[symbol][interval].iloc[position - 1]
            upper = float(previous["bb_upper"])
            lower = float(previous["bb_lower"])
            touched_upper = float(bar["high"]) >= upper
            touched_lower = float(bar["low"]) <= lower
            if touched_upper == touched_lower:
                continue
            side = "SHORT" if touched_upper else "LONG"
            reference = upper if touched_upper else lower
            entry_filter = micro_decision(
                symbol,
                side,
                reference,
                pd.Timestamp(bars.index[position]),
                interval,
            )
            if not entry_filter.allowed:
                continue
            open_position(
                side,
                symbol,
                interval,
                reference,
                float(previous["bb_middle"]),
                pd.Timestamp(bars.index[position]),
                f"按15m→30m→1h选中{symbol} {interval}箱体并触轨；{entry_filter.reason}",
            )
            batch_traded_symbols.add(symbol)
            immediate_stop = (
                side == "LONG" and float(bar["low"]) <= symbol_position.stop_price
            ) or (side == "SHORT" and float(bar["high"]) >= symbol_position.stop_price)
            if immediate_stop:
                close_quantity(
                    symbol,
                    symbol_position.remaining_quantity,
                    symbol_position.stop_price,
                    pd.Timestamp(bars.index[position]),
                    "触轨开仓后同根K线止损",
                    market=True,
                )
                symbol_position.position_side = ""
                symbol_position.active_interval = ""
                _block_symbol_after_stop(
                    state,
                    symbol,
                    interval,
                    pd.Timestamp(bars.index[position]),
                )

        if state.equity / state.day_start_equity - 1.0 <= -0.02:
            if not state.daily_blocked:
                add_event(
                    close_time,
                    "模拟账户触发每日亏损熔断",
                    "ACCOUNT",
                    "ACCOUNT",
                    f"当日权益变化：{state.equity / state.day_start_equity - 1.0:.2%}",
                    severity="CRITICAL",
                )
            state.daily_blocked = True
        state.peak_equity = max(state.peak_equity, state.equity)
        drawdown = 1.0 - state.equity / state.peak_equity
        if drawdown >= 0.08 and not state.permanent_fuse:
            state.permanent_fuse = True
            add_event(
                close_time,
                "模拟账户触发8%最大回撤熔断",
                "ACCOUNT",
                "ACCOUNT",
                f"当前回撤：{drawdown:.2%}",
                severity="CRITICAL",
            )
        # 当前主周期K线已经在close_time完整结束，其三轨状态从此刻起可供下一分钟使用。
        # 这一步修复旧版使用previous导致震荡资格额外晚一根主周期K线的问题。
        for symbol, interval, position in batch:
            current = contexts[symbol][interval].iloc[position]
            _refresh_box_qualification(
                state,
                symbol,
                interval,
                box_candidate=bool(current["box_candidate"]),
                breakout=bool(current["breakout"]),
            )
        for symbol, interval, position in batch:
            state.last_bar_times[symbol][interval] = (
                bars_by_symbol[symbol][interval].index[position].isoformat()
            )
            processed_bars += 1

    _save_state(state_path, state)
    selected = next(
        (
            (symbol, interval)
            for interval in ENTRY_INTERVAL_PRIORITY
            for symbol in symbols
            if state.box_active[symbol][interval] and not state.blocked_after_stop[symbol][interval]
        ),
        ("", ""),
    )
    active = [
        (symbol, position.position_side, position.active_interval)
        for symbol, position in state.positions.items()
        if position.position_side
    ]
    return MultiTimeframePaperSummary(
        status="FUSED" if state.permanent_fuse else "RUNNING",
        processed_bars=processed_bars,
        events=tuple(events),
        equity=state.equity,
        position_side=(active[0][1] if len(active) == 1 else "MULTIPLE" if active else ""),
        active_symbol=",".join(item[0] for item in active),
        active_interval=",".join(item[2] for item in active),
        selected_symbol=selected[0],
        selected_interval=selected[1],
        reason="V5.7按1分钟触轨即时执行，不等待主周期收线；Gate订单提交接口未调用",
    )
