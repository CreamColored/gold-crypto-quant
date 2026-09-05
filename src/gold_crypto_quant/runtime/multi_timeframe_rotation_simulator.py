"""按5m、15m、30m、1h优先级运行并由1分钟触轨执行的布林带影子模拟器。"""

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from gold_crypto_quant.runtime.bollinger_rotation_simulator import RotationPaperEvent
from gold_crypto_quant.strategy.bollinger_range import (
    build_rotation_box_context,
    parameters_for_same_timeframe,
)

MULTI_ROTATION_STRATEGY_VERSION = "5.8.0"
INTERVAL_PRIORITY = ("5m", "15m", "30m", "1h")
# 5分钟不再直接触发交易，但仍留在 INTERVAL_PRIORITY 里——顶底结构要查全部七个周期，
# 把它从那里拿掉会连结构判定一起丢掉。已经持有的5m仓位照常按原周期管理到结束。
ENTRY_INTERVAL_PRIORITY = ("15m", "30m", "1h")
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
# 止损距离改为不分周期的固定点数。仓位按 equity×risk_per_trade÷止损距离 反推，
# 因此放宽止损等于缩小仓位，每单风险仍是账户的 0.25%，不会因为止损变宽而多亏。
FIXED_STOP_DISTANCE = {
    "BTC_USDT": {"5m": 300.0, "15m": 300.0, "30m": 300.0, "1h": 300.0},
    "ETH_USDT": {"5m": 12.0, "15m": 12.0, "30m": 12.0, "1h": 12.0},
}
# 其余品种按125倍杠杆下浮亏100%换算：价格反向波动 1.00/125 = 0.8%。
OTHER_SYMBOL_STOP_RETURN = 1.00

# 中轨减仓比例。第一次减仓落在中轨，无论有没有顶底结构都是30%。
MIDDLE_REDUCE_RATIO = 0.30
# 第二次及以后的减仓——对侧轨结构确认、以及结构延续阶梯——仍是50%。
LADDER_REDUCE_RATIO = 0.50

# 手续费按币安与Gate的U本位永续VIP0公开费率：挂单0.02%、吃单0.05%。
# 不要改回负的maker费率——那是VIP4以上才有的挂单返佣，普通账户拿不到。
# 2026-09-03实测：当天73笔成交、名义额32.2万U，返佣假设让手续费少算47.95U，
# 足以把当天的+18.73U翻成-29.22U。
# 最大回撤熔断阈值。烧掉之后是**永久的**，没有恢复路径——2025-09 到 2026-09 的
# 一年回放里，第 17-35 天就触发了，剩下 330 天一笔没开。
# 提成常量是为了让回放能覆盖它：研究策略本身时要把风控闸门摘掉，
# 否则量到的是闸门什么时候关，不是策略好不好。线上必须保持 0.08。
MAX_DRAWDOWN_FUSE = 0.08

MAKER_FEE_RATE = 0.0002
TAKER_FEE_RATE = 0.0005


@dataclass(slots=True)
class SymbolPaperPositionState:
    """单一品种跨周期共享的唯一仓位状态。"""

    position_side: str = ""
    active_interval: str = ""
    # 开仓时刻的ISO字符串，用于在平仓事件里给出持仓时长。
    entry_time: str = ""
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
    # 止损阶梯依次落在：开仓价 → 开仓时中轨 → 开仓时对侧轨 → 上一次减仓价。
    entry_opposite_band: float = 0.0
    trend_ride_last_reduce_price: float = 0.0
    trend_ride_steps_done: int = 0
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
    # 在途K线的触轨条件首次成立的时刻。条件连续成立满 provisional_dwell_seconds 秒
    # 才真正开仓——BTC 每分钟的最高点有两成只停留了一秒，这些插针不值得进场。
    # 条件一旦断掉就清空，重新计时。
    provisional_touch_since: dict[str, str] = field(default_factory=dict)
    # 当前这一仓是在哪根1分钟K线内开的。该K线收线后带着完整极值再次进入循环时，
    # 那些极值可能发生在开仓之前——用入场前的价格止损是错的，因此跳过这一根的
    # 止损判定，从下一根开始正常判。
    opened_in_bar: dict[str, str] = field(default_factory=dict)
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
    # 供通知直接展示的当前持仓摘要；无持仓时为"全部空仓"。
    holdings: str = ""
    # 本轮在途K线的观察情况：哪些品种正在轨道外计时、已经多久。
    # 每秒轮询下这是唯一能看出"那59轮在干什么"的信息，否则日志里只有权益没变。
    provisional_watch: str = ""


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
    if stored_version in {"5.3.0", "5.4.0", "5.5.0", "5.6.0", "5.7.0"}:
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


# 顶/底结构按MACD红绿柱分组比较：
#   顶部结构＝本组红柱区收盘价更高、但DIF更低，在红柱区结束（柱值转负）那根K线确认；
#   底部结构＝本组绿柱区收盘价更低、但DIF更高，在绿柱区结束（柱值转正）那根K线确认。
# 判定是离散的——只在柱区刚结束时成立一次，不是每根K线都重复判定。
STRUCTURE_MACD_FAST = 12
STRUCTURE_MACD_SLOW = 26
STRUCTURE_MACD_SIGNAL = 9
# 计算MACD需要足够长的历史，取150根让EMA26充分收敛。
STRUCTURE_MACD_HISTORY = 150
# 顶底结构要覆盖的全部周期；做任何一个周期的震荡都要检查这七个。
STRUCTURE_INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}
STRUCTURE_LADDER_STEP_POINTS = 8.0


def _base_asset(symbol: str) -> str:
    """取计价前的基础资产代码，用于在通知里标注数量单位。"""
    return symbol.split("_", 1)[0]


def _format_duration(delta: pd.Timedelta) -> str:
    """把持仓时长渲染成中文短句；不足一分钟按分钟向上取整显示。"""
    minutes = max(1, int(round(delta.total_seconds() / 60)))
    if minutes < 60:
        return f"{minutes}分钟"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{rest}分钟" if rest else f"{hours}小时"
    days, rest_hours = divmod(hours, 24)
    return f"{days}天{rest_hours}小时" if rest_hours else f"{days}天"


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


def _macd_frame(bars: pd.DataFrame) -> tuple:
    """按标准MACD(12,26,9)返回收盘价、DIF与柱值三条序列。"""
    close = pd.to_numeric(bars["close"], errors="raise").astype(float)
    fast = close.ewm(span=STRUCTURE_MACD_FAST, adjust=False).mean()
    slow = close.ewm(span=STRUCTURE_MACD_SLOW, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=STRUCTURE_MACD_SIGNAL, adjust=False).mean()
    return close.to_numpy(), dif.to_numpy(), ((dif - dea) * 2).to_numpy()


# 反手开仓的额外过滤钩子。默认 None＝不过滤，线上行为与未引入该钩子时完全一致；
# 只有回测显式赋值时才生效，用来在不改动线上策略的前提下试验规则。
# 签名：(symbol, interval, new_side, moment, bars) -> 是否允许反手
REVERSAL_FILTER = None


def current_histogram_run(bars: pd.DataFrame) -> tuple[int, int]:
    """当前这一组MACD柱体的方向和长度。

    返回 (方向, 根数)：方向 +1 为红柱（柱值为正）、-1 为绿柱，根数是连续同号的
    柱子个数。刚变色时根数为1。数据不足时返回 (0, 0)。
    """
    if len(bars) < STRUCTURE_MACD_SLOW * 2:
        return (0, 0)
    _close, _dif, histogram = _macd_frame(bars.tail(STRUCTURE_MACD_HISTORY))
    if len(histogram) == 0:
        return (0, 0)
    sign = 1 if histogram[-1] > 0 else -1 if histogram[-1] < 0 else 0
    if sign == 0:
        return (0, 0)
    run = 0
    for value in reversed(list(histogram)):
        if (value > 0) == (sign > 0) and value != 0:
            run += 1
        else:
            break
    return (sign, run)


def _histogram_groups(histogram, *, positive: bool) -> list[tuple[int, int]]:
    """把MACD柱按符号切成连续的红柱区或绿柱区，返回每段的[起,止]下标。"""
    groups: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(histogram):
        matches = value > 0 if positive else value < 0
        if matches and start is None:
            start = index
        elif not matches and start is not None:
            groups.append((start, index - 1))
            start = None
    if start is not None:
        groups.append((start, len(histogram) - 1))
    return groups


def _structure_confirmed(bars: pd.DataFrame, *, top: bool) -> bool:
    """判断顶/底结构此刻是否处于生效状态。

    顶部结构在死叉那根K线成立：本组红柱区最高收盘价高于上一组，但组内DIF最大值反而更低。
    成立后一直有效，直到再次金叉才消失；底部结构对称，由金叉确立、死叉解除。

    因此这是一个持续状态而不是瞬时信号——死叉后的整段负柱区间里，顶部结构都算有效。
    """
    if len(bars) < STRUCTURE_MACD_SLOW * 2:
        return False
    window = bars.tail(STRUCTURE_MACD_HISTORY)
    close, dif, histogram = _macd_frame(window)
    if len(histogram) < 2:
        return False
    # 顶部结构只在死叉之后（柱为负）的区间里有效，金叉一旦出现即失效。
    if top and histogram[-1] > 0:
        return False
    if not top and histogram[-1] < 0:
        return False
    groups = _histogram_groups(histogram, positive=top)
    if len(groups) < 2:
        return False
    (prev_start, prev_end), (last_start, last_end) = groups[-2], groups[-1]
    # 最近一组必须紧邻当前反向区间，确保比较的是刚结束的那次交叉。
    if last_end >= len(histogram) - 1:
        return False
    prev_close = close[prev_start : prev_end + 1]
    last_close = close[last_start : last_end + 1]
    prev_dif = dif[prev_start : prev_end + 1]
    last_dif = dif[last_start : last_end + 1]
    if top:
        return bool(last_close.max() > prev_close.max() and last_dif.max() < prev_dif.max())
    return bool(last_close.min() < prev_close.min() and last_dif.min() > prev_dif.min())


def _has_top_structure(bars: pd.DataFrame) -> bool:
    """顶部结构：红柱区结束时，本组收盘价更高但DIF更低。"""
    return _structure_confirmed(bars, top=True)


def _has_bottom_structure(bars: pd.DataFrame) -> bool:
    """底部结构：绿柱区结束时，本组收盘价更低但DIF更高。"""
    return _structure_confirmed(bars, top=False)


def _provisional_dwell_ready(
    state: "MultiTimeframePaperState",
    symbol: str,
    qualified: bool,
    now: datetime,
    dwell_seconds: float,
) -> bool:
    """在途触轨是否已经连续成立够久。

    条件首次成立时记下时刻，之后每秒复查：仍然成立就看够不够 ``dwell_seconds``，
    断掉就清空重新计时。用时刻而不是计数，是因为轮次可能被跳过或变慢，"评估了三次"
    不等于"成立了三秒"。

    只用于在途K线。收线K线代表整整一分钟，本身已是充分观察，不加这道闸。
    """
    if not qualified:
        state.provisional_touch_since.pop(symbol, None)
        return False
    raw = state.provisional_touch_since.get(symbol)
    if not raw:
        state.provisional_touch_since[symbol] = now.isoformat()
        return dwell_seconds <= 0
    try:
        since = datetime.fromisoformat(raw)
    except ValueError:
        state.provisional_touch_since[symbol] = now.isoformat()
        return dwell_seconds <= 0
    return (now - since).total_seconds() >= dwell_seconds


# 指标与重采样全是收线K线的纯函数，而收线K线一分钟才变一次。改成每秒轮询之后，
# 一分钟内的六十次调用会把同样的布林带、MACD和重采样重算六十遍——线上占掉近半个
# 核心，逐秒回测更是直接不可行（24小时要跑51分钟）。
#
# 指纹用长度＋首尾时间＋首尾收盘价：K线一旦收线就不再变动，追加新K线会改变长度和
# 末根时间，滑动窗口会改变首根时间。全是 O(1)，比重算便宜几个数量级。
_CONTEXT_CACHE: dict[tuple, pd.DataFrame] = {}
_CONTEXT_CACHE_LIMIT = 128


def _frame_fingerprint(frame: pd.DataFrame) -> tuple:
    if frame.empty:
        return (0,)
    close = frame["close"]
    return (
        len(frame),
        frame.index[0].value,
        frame.index[-1].value,
        float(close.iloc[0]),
        float(close.iloc[-1]),
    )


def _cached(key: tuple, build) -> pd.DataFrame:
    """按指纹缓存纯函数结果；超出上限就整体清空，不做LRU簿记。

    条目数只有品种×周期的量级（十几条），到不了上限；上限只是防止回测里滑动窗口
    把字典撑大。整体清空比维护淘汰顺序更简单，代价是偶尔多算一轮。
    """
    hit = _CONTEXT_CACHE.get(key)
    if hit is not None:
        return hit
    if len(_CONTEXT_CACHE) >= _CONTEXT_CACHE_LIMIT:
        _CONTEXT_CACHE.clear()
    value = build()
    _CONTEXT_CACHE[key] = value
    return value


def reset_context_cache() -> None:
    """清空指标缓存；测试与回测切换数据源时调用。"""
    _CONTEXT_CACHE.clear()


def _resample_minutes(bars_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """把1分钟K线按UTC自然边界聚合到指定分钟周期，不产生未来数据。"""
    return (
        bars_1m.resample(f"{minutes}min", origin="epoch", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
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
    maker_fee_rate: float = MAKER_FEE_RATE,
    taker_fee_rate: float = TAKER_FEE_RATE,
    stop_slippage_rate: float = 0.0002,
    risk_per_trade: float = 0.0025,
    minimum_reward_risk: float = 0.0,
    provisional_by_symbol: "dict[str, pd.Series] | None" = None,
    provisional_dwell_seconds: float = 3.0,
    now: "datetime | None" = None,
    entry_allowed: "Callable[[str], bool] | None" = None,
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
            interval: _cached(
                ("box", symbol, interval, _frame_fingerprint(bars),
                 repr(parameters_by_market[symbol][interval])),
                lambda bars=bars, symbol=symbol, interval=interval: build_rotation_box_context(
                    bars, parameters_by_market[symbol][interval]
                ),
            )
            for interval, bars in symbol_bars.items()
        }
        for symbol, symbol_bars in bars_by_symbol.items()
    }
    micro_contexts: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    micro_three_minute_bars: dict[str, pd.DataFrame] = {}
    # 顶底结构要查全部七个周期，1m/3m/10m 由1分钟重采样得到，其余用主周期K线。
    structure_bars: dict[str, dict[str, pd.DataFrame]] = {}
    if micro_bars_by_symbol is not None:
        missing_micro = set(symbols).difference(micro_bars_by_symbol)
        if missing_micro:
            raise ValueError(f"missing 1m bars for symbols: {sorted(missing_micro)}")
        for symbol in symbols:
            bars_1m = micro_bars_by_symbol[symbol]
            if len(bars_1m) < 65:
                raise ValueError(f"{symbol} requires at least 65 one-minute bars")
            micro_key = _frame_fingerprint(bars_1m)
            bars_3m = _cached(
                ("3m", symbol, micro_key),
                lambda bars_1m=bars_1m: _resample_three_minute_bars(bars_1m),
            )
            micro_three_minute_bars[symbol] = bars_3m
            structure_bars[symbol] = {
                "1m": bars_1m,
                "3m": bars_3m,
                "10m": _cached(
                    ("10m", symbol, micro_key),
                    lambda bars_1m=bars_1m: _resample_minutes(bars_1m, 10),
                ),
                **{
                    interval: bars_by_symbol[symbol][interval]
                    for interval in INTERVAL_PRIORITY
                    if interval in bars_by_symbol[symbol]
                },
            }
            micro_contexts[symbol] = (
                bars_1m,
                _cached(
                    ("mb1", symbol, micro_key),
                    lambda bars_1m=bars_1m: _micro_bollinger_context(bars_1m),
                ),
                _cached(
                    ("mb3", symbol, micro_key, _frame_fingerprint(bars_3m)),
                    lambda bars_3m=bars_3m: _micro_bollinger_context(bars_3m),
                ),
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
            provisional_watch="",
            # 首次建账必然无持仓；不写死会让通知里出现空白的"持仓："一行。
            holdings="全部空仓",
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
        position.entry_time = timestamp.isoformat()
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
        position.trend_ride_last_reduce_price = 0.0
        position.trend_ride_steps_done = 0
        # 布林带上下轨对中轨严格对称，因此对侧轨＝2×中轨−入场轨，无需另存一份上下文。
        position.entry_opposite_band = 2.0 * middle_reference - reference
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
        stop_points = abs(reference - position.stop_price)
        add_event(
            timestamp,
            f"模拟开仓：{'买入做多' if side == 'LONG' else '卖出做空'}",
            symbol,
            interval,
            f"原因：{reason}",
            f"订单动作：{'买入开多' if side == 'LONG' else '卖出开空'}",
            f"轨道限价：{reference:.2f}",
            f"数量：{position.quantity:.6f} {_base_asset(symbol)}",
            f"名义价值：{reference * position.quantity:,.2f} USDT",
            (
                f"本单风险：{state.equity * risk_per_trade:.2f} USDT"
                f"（账户{risk_per_trade:.2%}）"
            ),
            (
                f"保护止损：{position.stop_price:.2f}"
                f"（{stop_points:.2f}点 / {stop_points / reference:.2%}"
                f" / 杠杆{stop_points / reference * PAPER_LEVERAGE:.0%}）"
            ),
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
        closed_all = position.remaining_quantity <= 1e-12
        going_long = position.position_side == "LONG"
        move_points = fill - position.entry_price if going_long else position.entry_price - fill
        share = quantity / position.quantity if position.quantity else 0.0
        base = _base_asset(symbol)
        held = ""
        if position.entry_time:
            held = _format_duration(timestamp - pd.Timestamp(position.entry_time))
        add_event(
            timestamp,
            f"模拟{'平仓' if closed_all else '减仓'}：{reason}",
            symbol,
            interval,
            f"订单动作：{'卖出平多' if going_long else '买入平空'}",
            f"成交参考价：{fill:.2f}",
            f"成交数量：{quantity:.6f} {base}（占原仓位{share:.0%}）",
            f"名义价值：{fill * quantity:,.2f} USDT",
            f"开仓价：{position.entry_price:.2f}",
            (
                f"价差：{move_points:+.2f}点 / {move_points / position.entry_price:+.2%}"
                f" / 杠杆{move_points / position.entry_price * PAPER_LEVERAGE:+.1%}"
            ),
            *((f"持仓时长：{held}",) if held else ()),
            (
                "剩余仓位：已全部了结"
                if closed_all
                else f"剩余仓位：{position.remaining_quantity:.6f} {base}"
            ),
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

    def structure_side(symbol: str, as_of: pd.Timestamp) -> str:
        """返回此刻生效的结构方向：LONG=底部结构、SHORT=顶部结构、空串=没有结构。

        七个周期（1m/3m/5m/10m/15m/30m/1h）任一命中即算成立，与交易周期无关——
        做5分钟震荡也要看1小时，做1小时也要看1分钟。
        """
        series = structure_bars.get(symbol)
        if not series:
            return ""
        found_top = found_bottom = False
        for name, bars in series.items():
            minutes = STRUCTURE_INTERVAL_MINUTES.get(name)
            if minutes is None or bars is None:
                continue
            closed = _closed_bars_before(
                bars, pd.Timedelta(minutes=minutes), as_of, STRUCTURE_MACD_HISTORY
            )
            if len(closed) < STRUCTURE_MACD_SLOW * 2:
                continue
            if not found_top and _has_top_structure(closed):
                found_top = True
            if not found_bottom and _has_bottom_structure(closed):
                found_bottom = True
        # 顶底同时出现时不给方向，避免多空两边都被判定成"顺结构"。
        if found_top == found_bottom:
            return ""
        return "SHORT" if found_top else "LONG"

    def has_reversal_structure(
        symbol: str,
        interval: str,
        new_side: str,
        as_of: pd.Timestamp,
    ) -> bool:
        """开仓方向与生效结构同向时返回真——该仓位改走结构延续，不再止盈反手。"""
        return structure_side(symbol, as_of) == new_side

    def structure_blocks_entry(
        symbol: str,
        interval: str,
        side: str,
        as_of: pd.Timestamp,
    ) -> bool:
        """开仓方向与生效结构相反时否决：有底部结构不做空、有顶部结构不做多。"""
        active = structure_side(symbol, as_of)
        return bool(active) and active != side

    def switch_allows_entry(symbol: str) -> bool:
        """交易开关关闭时禁止开仓；已有仓位的止损、减仓与止盈照常执行。

        语义是"只出不进"，不是冻结持仓——关掉开关不应该让一笔在场的单子失去
        止损保护。因此这个判定只挂在两个开仓入口上，平仓路径完全不受影响。
        """
        return entry_allowed is None or entry_allowed(symbol)

    def entry_reward_is_acceptable(
        symbol: str,
        interval: str,
        side: str,
        reference: float,
        middle_reference: float,
    ) -> bool:
        """第一个收益目标离得太近就不开这一单——开仓那一刻赔率已经是负的。

        止损是写死的固定点数（BTC 5m 250点、ETH 5点、XAU按0.32%换算），
        而到中轨的距离由布林带宽度决定，两者之间没有任何约束关系。箱体窄的时候
        到中轨可能只有2点而止损仍是14点，做对了赚不回手续费、做错了亏满一格。

        2026-09-04 当天XAU反复出现这种单子：赔率0.19:1到0.32:1，
        十几笔下来净贡献接近于零，纯粹在交手续费。

        门槛为0时不做任何过滤，行为与启用前完全一致。
        """
        if minimum_reward_risk <= 0:
            return True
        parameters = parameters_by_market[symbol][interval]
        trigger, _advance = _middle_reduction_trigger(
            symbol, side, reference, middle_reference, 1.0
        )
        reward = abs(trigger - reference)
        return reward >= parameters.fixed_stop_distance * minimum_reward_risk

    def refresh_structure_flag(symbol: str, as_of: pd.Timestamp) -> None:
        """持仓期间实时复查结构：开仓时没有、持仓中出现同向结构，一样转入延续模式。

        结构可能在开仓后、甚至反手之后才出现，因此每分钟都要重新确认，
        而不是只在开仓那一刻判定一次。一旦确认就保持，避免结构短暂消失导致来回切换。
        """
        position = state.positions[symbol]
        if not position.position_side or position.structure_confirmed:
            return
        if structure_side(symbol, as_of) == position.position_side:
            position.structure_confirmed = True

    def stop_interval_open_time(timestamp: pd.Timestamp, interval: str) -> pd.Timestamp:
        """把逐分钟止损时间归属到对应主周期K线，供完整收线等待规则使用。"""
        frequency = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}[interval]
        return timestamp.floor(frequency)

    # V5.7把已确认箱体后的触轨、止损、减仓和止盈全部下沉到1分钟。
    # 行情服务每分钟调用一次，因此不必等待15m/30m/1h收线才发送交易事件。
    if micro_contexts and not micro_cursor_initialized:
        wall_clock = now or datetime.now(UTC)
        # 元组第四位标记这根是不是在途K线（尚未收线、用盘口合成）。
        minute_items: list[tuple[pd.Timestamp, str, int, bool]] = []
        for symbol in symbols:
            bars_1m = micro_contexts[symbol][0]
            last_micro_time = pd.Timestamp(state.last_micro_bar_times[symbol])
            for position, minute_open_time in enumerate(bars_1m.index):
                if minute_open_time > last_micro_time:
                    minute_items.append((pd.Timestamp(minute_open_time), symbol, position, False))
            # 在途K线排在所有收线K线之后：它覆盖的是当前这一分钟，时间上最新。
            provisional = (provisional_by_symbol or {}).get(symbol)
            if provisional is not None:
                provisional_time = pd.Timestamp(provisional.name)
                if provisional_time > last_micro_time:
                    minute_items.append((provisional_time, symbol, -1, True))

        for minute_open_time, symbol, minute_position, is_provisional in sorted(
            minute_items,
            key=lambda item: (item[0], item[3], symbols.index(item[1])),
        ):
            bars_1m = micro_contexts[symbol][0]
            minute_bar = (
                (provisional_by_symbol or {})[symbol]
                if is_provisional
                else bars_1m.iloc[minute_position]
            )
            position = state.positions[symbol]
            traded_this_minute = False

            # 已持仓时先执行保护止损，再判断对侧轨、减仓；风险动作永远优先于新开仓。
            if position.position_side:
                # 情况2/3：结构可能在开仓之后、或反手之后才形成，因此每分钟都要复查一次。
                refresh_structure_flag(symbol, minute_open_time)
                interval = position.active_interval
                main = confirmed_context_at(symbol, interval, minute_open_time)
                if main is not None:
                    upper = float(main["bb_upper"])
                    lower = float(main["bb_lower"])
                    # 开仓所在的那一根收线后会带着整分钟的极值回到这里，而其中一部分
                    # 发生在开仓之前。跳过这一根的止损判定，从下一根开始正常判——
                    # 开仓当时的即时止损在 open_position 之后已经单独判过了。
                    same_bar_as_entry = (
                        not is_provisional
                        and state.opened_in_bar.get(symbol) == minute_open_time.isoformat()
                    )
                    stop_hit = not same_bar_as_entry and (
                        (
                            position.position_side == "LONG"
                            and float(minute_bar["low"]) <= position.stop_price
                        )
                        or (
                            position.position_side == "SHORT"
                            and float(minute_bar["high"]) >= position.stop_price
                        )
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
                                position.remaining_quantity * LADDER_REDUCE_RATIO,
                                trigger,
                                minute_open_time,
                                f"结构延续：每{step:.2f}点阶梯减仓{LADDER_REDUCE_RATIO:.0%}",
                                market=False,
                            )
                            going_long = position.position_side == "LONG"
                            # 止损滞后一档：第一次阶梯减仓后落在开仓时的对侧轨，
                            # 之后每次都落在上一次减仓的价格上。
                            if position.trend_ride_steps_done == 0 and position.entry_opposite_band:
                                position.stop_price = position.entry_opposite_band
                            else:
                                position.stop_price = position.trend_ride_last_reduce_price
                            position.trend_ride_last_reduce_price = trigger
                            position.trend_ride_steps_done += 1
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
                                position.remaining_quantity * LADDER_REDUCE_RATIO,
                                target,
                                minute_open_time,
                                f"对侧轨结构确认：减仓{LADDER_REDUCE_RATIO:.0%}延续原方向，"
                                f"不反手（步长{step:.2f}点）",
                                market=False,
                            )
                            # 止损收到开仓当时的中轨，而不是随后漂移过的当前中轨。
                            position.stop_price = (
                                position.middle_reference_price or float(main["bb_middle"])
                            )
                            position.trend_ride_active = True
                            position.trend_ride_step_points = step
                            position.trend_ride_last_reduce_price = target
                            position.trend_ride_steps_done = 0
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
                            new_side = "SHORT" if old_side == "LONG" else "LONG"
                            reversal_allowed = REVERSAL_FILTER is None or REVERSAL_FILTER(
                                symbol,
                                interval,
                                new_side,
                                minute_open_time,
                                bars_by_symbol[symbol][interval],
                            )
                            if (
                                box_valid
                                and reversal_allowed
                                and switch_allows_entry(symbol)
                                and not structure_blocks_entry(
                                    symbol, interval, new_side, minute_open_time
                                )
                                and entry_reward_is_acceptable(
                                    symbol, interval, new_side, target,
                                    float(main["bb_middle"]),
                                )
                            ):
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
                            percent = f"{MIDDLE_REDUCE_RATIO:.0%}"
                            middle_reason = (
                                f"距离中轨{position.middle_advance_distance:.2f}点"
                                f"提前减仓{percent}"
                                if position.middle_advance_distance > 0
                                else f"到达中轨减仓{percent}"
                            )
                            close_quantity(
                                symbol,
                                position.remaining_quantity * MIDDLE_REDUCE_RATIO,
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
                    side = "SHORT" if touched_upper else "LONG"
                    reference = upper if touched_upper else lower
                    qualified = (
                        touched_upper != touched_lower
                        and switch_allows_entry(symbol)
                        and not structure_blocks_entry(
                            symbol, interval, side, minute_open_time
                        )
                        and entry_reward_is_acceptable(
                            symbol, interval, side, reference, float(main["bb_middle"])
                        )
                    )
                    if is_provisional:
                        # 停留确认必须看**当前价**而不是累计极值。在途K线的high/low
                        # 是一分钟内的累计最值，只增不减——拿它判定的话，一秒的插针
                        # 把high顶上去之后条件就永远成立，闸门只会把开仓延后几秒，
                        # 完全起不到过滤作用。用close（最新中间价）才能表达"价格现在
                        # 还在轨道外"，弹回来立刻清零重新计时。
                        close_now = float(minute_bar["close"])
                        still_outside = (side == "SHORT" and close_now >= upper) or (
                            side == "LONG" and close_now <= lower
                        )
                        qualified = _provisional_dwell_ready(
                            state,
                            symbol,
                            qualified and still_outside,
                            wall_clock,
                            provisional_dwell_seconds,
                        )
                    if qualified:
                        open_position(
                            side,
                            symbol,
                            interval,
                            reference,
                            float(main["bb_middle"]),
                            minute_open_time,
                            f"{interval}震荡箱体触及{'上轨' if touched_upper else '下轨'}"
                            # 标出触发来源：不标的话事后分不清这一单是在途K线触发的
                            # 还是收线才触发的，方案B到底起没起作用就无从验证。
                            + ("在途触发开仓" if is_provisional else "即时开仓"),
                        )
                        # 记下开仓所在的K线。这一根收线后带着完整极值再来时，那些极值
                        # 可能发生在开仓之前——用入场前的价格止损是错的。
                        state.opened_in_bar[symbol] = minute_open_time.isoformat()
                        state.provisional_touch_since.pop(symbol, None)
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
            if not is_provisional:
                # 在途K线绝不推进游标：推过去之后，这一分钟收线时带着真实最高最低价
                # 再来，就会因为"不比游标新"而被整根跳过——用几秒钟的残缺数据做了
                # 决策，整分钟的真实极值被静默丢弃。
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
        # 解除封锁本身会改写状态，因此即使不再需要返回值也必须逐个调用。
        for symbol in symbols:
            _try_release_symbol_after_wait(
                state,
                symbol,
                close_time,
                batch,
                contexts,
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
        if drawdown >= MAX_DRAWDOWN_FUSE and not state.permanent_fuse:
            state.permanent_fuse = True
            add_event(
                close_time,
                f"模拟账户触发{MAX_DRAWDOWN_FUSE:.0%}最大回撤熔断",
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

    def last_price(symbol: str) -> float:
        """取该品种最新收盘价；1分钟序列最新，缺失时退回主周期。"""
        if micro_contexts and symbol in micro_contexts:
            frame = micro_contexts[symbol][0]
            if not frame.empty:
                return float(frame["close"].iloc[-1])
        return float(bars_by_symbol[symbol][INTERVAL_PRIORITY[0]]["close"].iloc[-1])

    def holding_text(symbol: str) -> str:
        """把单个品种的在场仓位压缩成一句话，含浮动盈亏。"""
        position = state.positions[symbol]
        mark = last_price(symbol)
        floating = (
            (mark - position.entry_price) * position.remaining_quantity
            if position.position_side == "LONG"
            else (position.entry_price - mark) * position.remaining_quantity
        )
        return (
            f"{_base_asset(symbol)} {'多' if position.position_side == 'LONG' else '空'} "
            f"{position.remaining_quantity:.6f} @{position.entry_price:.2f} "
            f"现价{mark:.2f}（浮动{floating:+.2f}U）"
        )

    held = [holding_text(symbol) for symbol, _side, _interval in active]
    # 正在轨道外计时的品种。每秒轮询下这是唯一能看出"那59轮在干什么"的信息——
    # 没有它，日志里只剩"权益没变、信号0条"，看不出停留确认到底有没有在工作。
    watching = []
    for symbol, raw in sorted(state.provisional_touch_since.items()):
        try:
            held_for = (wall_clock - datetime.fromisoformat(raw)).total_seconds()
        except (ValueError, NameError):
            continue
        watching.append(f"{symbol}已在轨道外{held_for:.0f}秒")
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
        reason="V5.8按秒级在途K线触轨执行，不等待主周期收线；Gate订单提交接口未调用",
        holdings="；".join(held) if held else "全部空仓",
        provisional_watch="、".join(watching),
    )
