"""按5m、15m、30m、1h优先级运行的单持仓布林带影子模拟器。"""

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

MULTI_ROTATION_STRATEGY_VERSION = "5.0.0"
INTERVAL_PRIORITY = ("5m", "15m", "30m", "1h")
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


@dataclass(slots=True)
class MultiTimeframePaperState:
    """跨周期共享资金和持仓、各周期独立箱体状态。"""

    strategy_version: str = MULTI_ROTATION_STRATEGY_VERSION
    equity: float = 10_000.0
    peak_equity: float = 10_000.0
    day_start_equity: float = 10_000.0
    current_day: str = ""
    last_bar_times: dict[str, dict[str, str]] = field(default_factory=dict)
    box_active: dict[str, dict[str, bool]] = field(default_factory=dict)
    blocked_after_stop: dict[str, dict[str, bool]] = field(default_factory=dict)
    reset_streak: dict[str, dict[str, int]] = field(default_factory=dict)
    position_symbol: str = ""
    position_side: str = ""
    active_interval: str = ""
    quantity: float = 0.0
    remaining_quantity: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    entry_fee_remaining: float = 0.0
    trade_net_pnl: float = 0.0
    middle_reduced: bool = False
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
    )


def _load_state(path: Path) -> MultiTimeframePaperState | None:
    """读取V5多品种状态；不存在时由首次调用建立新账户。"""
    if not path.exists():
        return None
    state = MultiTimeframePaperState(**json.loads(path.read_text(encoding="utf-8")))
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
    if symbol != "BTC_USDT":
        return parameters
    # 调用最近20根收盘中位数，避免单根插针对换算比例产生明显影响。
    price_scale = max(
        1.0,
        float(pd.to_numeric(bars["close"].tail(20), errors="raise").median())
        / ETH_RULE_REFERENCE_PRICE,
    )
    return replace(
        parameters,
        maximum_band_drift=parameters.maximum_band_drift * price_scale,
        fixed_stop_distance=parameters.fixed_stop_distance * price_scale,
        minimum_bandwidth=parameters.minimum_bandwidth * price_scale,
    )


def run_multi_timeframe_paper_cycle(
    bars_by_interval: (dict[str, pd.DataFrame] | dict[str, dict[str, pd.DataFrame]]),
    *,
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
        # 新策略首次启动只建立两个品种、四个周期游标，禁止回放历史成交。
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
            reason="V5 BTC/ETH共享影子账户已从各周期最新收盘K线开始",
        )

    events: list[RotationPaperEvent] = []

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
        timestamp: pd.Timestamp,
        reason: str,
    ) -> None:
        """按全账户0.25%风险建立唯一仓位，并记录该仓位所属周期。"""
        parameters = parameters_by_market[symbol][interval]
        state.position_side = side
        state.position_symbol = symbol
        state.active_interval = interval
        state.entry_price = reference
        state.stop_price = (
            reference - parameters.fixed_stop_distance
            if side == "LONG"
            else reference + parameters.fixed_stop_distance
        )
        stop_fill = state.stop_price * (
            1.0 - stop_slippage_rate if side == "LONG" else 1.0 + stop_slippage_rate
        )
        adverse_loss = reference - stop_fill if side == "LONG" else stop_fill - reference
        loss_per_unit = adverse_loss + reference * maker_fee_rate + stop_fill * taker_fee_rate
        state.quantity = state.equity * risk_per_trade / loss_per_unit
        state.remaining_quantity = state.quantity
        state.entry_fee_remaining = reference * state.quantity * maker_fee_rate
        state.equity -= state.entry_fee_remaining
        state.trade_net_pnl = 0.0
        state.middle_reduced = False
        add_event(
            timestamp,
            f"模拟开仓：{'买入做多' if side == 'LONG' else '卖出做空'}",
            symbol,
            interval,
            f"原因：{reason}",
            f"轨道限价：{reference:.2f}",
            f"数量：{state.quantity:.6f} {symbol.split('_', 1)[0]}",
            f"保护止损：{state.stop_price:.2f}",
            f"影子权益：{state.equity:.2f} USDT",
        )

    def close_quantity(
        quantity: float,
        reference: float,
        timestamp: pd.Timestamp,
        reason: str,
        *,
        market: bool,
    ) -> None:
        """模拟唯一仓位的减仓或平仓，并把成本和盈亏计入共享权益。"""
        symbol = state.position_symbol
        interval = state.active_interval
        if market:
            fill = reference * (
                1.0 - stop_slippage_rate
                if state.position_side == "LONG"
                else 1.0 + stop_slippage_rate
            )
            fee_rate = taker_fee_rate
        else:
            fill = reference
            fee_rate = maker_fee_rate
        gross = (
            (fill - state.entry_price) * quantity
            if state.position_side == "LONG"
            else (state.entry_price - fill) * quantity
        )
        allocated_entry_fee = state.entry_fee_remaining * (quantity / state.remaining_quantity)
        exit_fee = fill * quantity * fee_rate
        net = gross - allocated_entry_fee - exit_fee
        state.equity += gross - exit_fee
        state.trade_net_pnl += net
        state.entry_fee_remaining -= allocated_entry_fee
        state.remaining_quantity -= quantity
        add_event(
            timestamp,
            f"模拟{'减仓' if state.remaining_quantity > 1e-12 else '平仓'}：{reason}",
            symbol,
            interval,
            f"方向：{state.position_side}",
            f"成交参考价：{fill:.2f}",
            f"本次净盈亏：{net:+.2f} USDT",
            f"整笔累计净盈亏：{state.trade_net_pnl:+.2f} USDT",
            f"影子权益：{state.equity:.2f} USDT",
            severity="WARNING" if net < 0 else "INFO",
        )

    # 把两个品种、四个周期按实际收盘时刻合并，同一时刻先周期再品种排序。
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
        batch_had_trade = False
        beijing_day = str(close_time.tz_convert("Asia/Shanghai").date())
        if state.current_day != beijing_day:
            state.current_day = beijing_day
            state.day_start_equity = state.equity
            state.daily_blocked = False

        # 每个品种、周期独立维护箱体和止损暂停，但它们共享唯一持仓。
        for symbol, interval, position in batch:
            if position < 1:
                continue
            previous = contexts[symbol][interval].iloc[position - 1]
            if bool(previous["breakout"]):
                state.box_active[symbol][interval] = False
                state.blocked_after_stop[symbol][interval] = True
                state.reset_streak[symbol][interval] = 0
            if state.blocked_after_stop[symbol][interval] and not state.permanent_fuse:
                state.reset_streak[symbol][interval] = (
                    state.reset_streak[symbol][interval] + 1
                    if bool(previous["box_candidate"])
                    else 0
                )
                if state.reset_streak[symbol][interval] >= 3:
                    state.blocked_after_stop[symbol][interval] = False
                    state.box_active[symbol][interval] = True
            elif bool(previous["box_candidate"]) and not state.permanent_fuse:
                state.box_active[symbol][interval] = True

        # 已有仓位只由其开仓周期管理，其他周期不能同时开相反仓位。
        active_item = next(
            (
                item
                for item in batch
                if item[0] == state.position_symbol and item[1] == state.active_interval
            ),
            None,
        )
        if state.position_side and active_item is not None:
            symbol, interval, position = active_item
            bars = bars_by_symbol[symbol][interval]
            bar = bars.iloc[position]
            previous = contexts[symbol][interval].iloc[position - 1]
            current = contexts[symbol][interval].iloc[position]
            upper = float(previous["bb_upper"])
            middle = float(previous["bb_middle"])
            lower = float(previous["bb_lower"])
            stop_hit = (
                state.position_side == "LONG" and float(bar["low"]) <= state.stop_price
            ) or (state.position_side == "SHORT" and float(bar["high"]) >= state.stop_price)
            target_hit = (state.position_side == "LONG" and float(bar["high"]) >= upper) or (
                state.position_side == "SHORT" and float(bar["low"]) <= lower
            )
            if stop_hit:
                stop_reason = "中轨减仓后的保护止损" if state.middle_reduced else "固定5点止损"
                close_quantity(
                    state.remaining_quantity,
                    state.stop_price,
                    pd.Timestamp(bars.index[position]),
                    stop_reason,
                    market=True,
                )
                batch_had_trade = True
                stopped_symbol = state.position_symbol
                stopped_interval = state.active_interval
                state.position_side = ""
                state.position_symbol = ""
                state.active_interval = ""
                state.blocked_after_stop[stopped_symbol][stopped_interval] = True
                state.box_active[stopped_symbol][stopped_interval] = False
                state.reset_streak[stopped_symbol][stopped_interval] = 0
            else:
                middle_hit = (state.position_side == "LONG" and float(bar["high"]) >= middle) or (
                    state.position_side == "SHORT" and float(bar["low"]) <= middle
                )
                if middle_hit and not state.middle_reduced and not target_hit:
                    close_quantity(
                        state.remaining_quantity * 0.5,
                        middle,
                        pd.Timestamp(bars.index[position]),
                        "到达中轨减仓50%",
                        market=False,
                    )
                    batch_had_trade = True
                    state.middle_reduced = True
                    state.stop_price = state.entry_price
                if target_hit and state.position_side:
                    old_side = state.position_side
                    target = upper if old_side == "LONG" else lower
                    close_quantity(
                        state.remaining_quantity,
                        target,
                        pd.Timestamp(bars.index[position]),
                        "到达对侧轨止盈",
                        market=False,
                    )
                    batch_had_trade = True
                    state.position_side = ""
                    # 当前K线仍然保持箱体才允许同品种、同周期反手；破轨只止盈。
                    current_box_valid = bool(current["box_candidate"]) and not bool(
                        current["breakout"]
                    )
                    if (
                        current_box_valid
                        and state.box_active[symbol][interval]
                        and not state.blocked_after_stop[symbol][interval]
                        and not state.daily_blocked
                        and not state.permanent_fuse
                    ):
                        new_side = "SHORT" if old_side == "LONG" else "LONG"
                        open_position(
                            new_side,
                            symbol,
                            interval,
                            target,
                            pd.Timestamp(bars.index[position]),
                            "对侧轨止盈后同周期立即反手",
                        )
                        immediate_stop = (
                            new_side == "LONG" and float(bar["low"]) <= state.stop_price
                        ) or (new_side == "SHORT" and float(bar["high"]) >= state.stop_price)
                        if immediate_stop:
                            close_quantity(
                                state.remaining_quantity,
                                state.stop_price,
                                pd.Timestamp(bars.index[position]),
                                "反手后同根K线止损",
                                market=True,
                            )
                            state.position_side = ""
                            state.position_symbol = ""
                            state.active_interval = ""
                            state.blocked_after_stop[symbol][interval] = True
                            state.box_active[symbol][interval] = False
                            state.reset_streak[symbol][interval] = 0
                    else:
                        state.position_symbol = ""
                        state.active_interval = ""

        # 空仓时按固定优先级选本批次中第一个有效周期，绝不多空双开。
        if (
            not state.position_side
            and not batch_had_trade
            and not state.daily_blocked
            and not state.permanent_fuse
        ):
            selected_item = next(
                (
                    (symbol, interval, position)
                    for symbol, interval, position in batch
                    if position >= 1
                    and state.box_active[symbol][interval]
                    and not state.blocked_after_stop[symbol][interval]
                ),
                None,
            )
            if selected_item is not None:
                symbol, interval, position = selected_item
                bars = bars_by_symbol[symbol][interval]
                bar = bars.iloc[position]
                previous = contexts[symbol][interval].iloc[position - 1]
                upper = float(previous["bb_upper"])
                lower = float(previous["bb_lower"])
                touched_upper = float(bar["high"]) >= upper
                touched_lower = float(bar["low"]) <= lower
                if touched_upper != touched_lower:
                    side = "SHORT" if touched_upper else "LONG"
                    reference = upper if touched_upper else lower
                    open_position(
                        side,
                        symbol,
                        interval,
                        reference,
                        pd.Timestamp(bars.index[position]),
                        f"按周期优先、BTC→ETH品种顺序选中{symbol} {interval}箱体并触轨",
                    )
                    batch_had_trade = True
                    immediate_stop = (side == "LONG" and float(bar["low"]) <= state.stop_price) or (
                        side == "SHORT" and float(bar["high"]) >= state.stop_price
                    )
                    if immediate_stop:
                        close_quantity(
                            state.remaining_quantity,
                            state.stop_price,
                            pd.Timestamp(bars.index[position]),
                            "触轨开仓后同根K线止损",
                            market=True,
                        )
                        state.position_side = ""
                        state.position_symbol = ""
                        state.active_interval = ""
                        state.blocked_after_stop[symbol][interval] = True
                        state.box_active[symbol][interval] = False
                        state.reset_streak[symbol][interval] = 0

        if state.equity / state.day_start_equity - 1.0 <= -0.02:
            if not state.daily_blocked:
                add_event(
                    close_time,
                    "模拟账户触发每日亏损熔断",
                    state.position_symbol or "ACCOUNT",
                    state.active_interval or "ACCOUNT",
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
                state.position_symbol or "ACCOUNT",
                state.active_interval or "ACCOUNT",
                f"当前回撤：{drawdown:.2%}",
                severity="CRITICAL",
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
            for interval in INTERVAL_PRIORITY
            for symbol in symbols
            if state.box_active[symbol][interval] and not state.blocked_after_stop[symbol][interval]
        ),
        ("", ""),
    )
    return MultiTimeframePaperSummary(
        status="FUSED" if state.permanent_fuse else "RUNNING",
        processed_bars=processed_bars,
        events=tuple(events),
        equity=state.equity,
        position_side=state.position_side,
        active_symbol=state.position_symbol,
        active_interval=state.active_interval,
        selected_symbol=selected[0],
        selected_interval=selected[1],
        reason="V5 BTC/ETH多周期共享单持仓影子模拟；Gate订单提交接口未调用",
    )
