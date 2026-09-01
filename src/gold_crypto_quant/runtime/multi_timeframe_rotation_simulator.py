"""按5m、15m、30m、1h优先级运行的单持仓布林带影子模拟器。"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from gold_crypto_quant.runtime.bollinger_rotation_simulator import RotationPaperEvent
from gold_crypto_quant.strategy.bollinger_range import (
    build_rotation_box_context,
    parameters_for_same_timeframe,
)

MULTI_ROTATION_STRATEGY_VERSION = "4.0.0"
INTERVAL_PRIORITY = ("5m", "15m", "30m", "1h")
INTERVAL_DURATION = {
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
}
DEFAULT_MULTI_STATE_PATH = Path(".runtime/bollinger-multi-timeframe-paper-v4.json")


@dataclass(slots=True)
class MultiTimeframePaperState:
    """跨周期共享资金和持仓、各周期独立箱体状态。"""

    strategy_version: str = MULTI_ROTATION_STRATEGY_VERSION
    equity: float = 10_000.0
    peak_equity: float = 10_000.0
    day_start_equity: float = 10_000.0
    current_day: str = ""
    last_bar_times: dict[str, str] = field(default_factory=dict)
    box_active: dict[str, bool] = field(default_factory=dict)
    blocked_after_stop: dict[str, bool] = field(default_factory=dict)
    reset_streak: dict[str, int] = field(default_factory=dict)
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
    active_interval: str
    selected_interval: str
    reason: str


def _new_state(initial_equity: float) -> MultiTimeframePaperState:
    """创建四个周期状态均已初始化的全新影子账户。"""
    return MultiTimeframePaperState(
        equity=initial_equity,
        peak_equity=initial_equity,
        day_start_equity=initial_equity,
        box_active={interval: False for interval in INTERVAL_PRIORITY},
        blocked_after_stop={interval: False for interval in INTERVAL_PRIORITY},
        reset_streak={interval: 0 for interval in INTERVAL_PRIORITY},
    )


def _load_state(path: Path) -> MultiTimeframePaperState | None:
    """读取V4状态；不存在时由首次调用建立新账户。"""
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
    temporary.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def run_multi_timeframe_paper_cycle(
    bars_by_interval: dict[str, pd.DataFrame],
    *,
    state_path: Path = DEFAULT_MULTI_STATE_PATH,
    initial_equity: float = 10_000.0,
    maker_fee_rate: float = -0.0001,
    taker_fee_rate: float = 0.00075,
    stop_slippage_rate: float = 0.0002,
    risk_per_trade: float = 0.0025,
) -> MultiTimeframePaperSummary:
    """按周期优先级逐批处理新K线，全局始终最多持有一个方向仓位。"""
    if set(bars_by_interval) != set(INTERVAL_PRIORITY):
        raise ValueError("multi-timeframe simulator requires 5m, 15m, 30m and 1h bars")
    for interval, bars in bars_by_interval.items():
        if len(bars) < 30:
            raise ValueError(f"{interval} requires at least 30 bars")
    contexts = {
        interval: build_rotation_box_context(
            bars, parameters_for_same_timeframe(interval)
        )
        for interval, bars in bars_by_interval.items()
    }
    state = _load_state(state_path)
    if state is None:
        state = _new_state(initial_equity)
        state.last_bar_times = {
            interval: bars.index[-1].isoformat()
            for interval, bars in bars_by_interval.items()
        }
        # 新策略首次启动只建立四个周期游标，禁止回放历史成交。
        _save_state(state_path, state)
        return MultiTimeframePaperSummary(
            status="INITIALIZED",
            processed_bars=0,
            events=(),
            equity=state.equity,
            position_side="",
            active_interval="",
            selected_interval="",
            reason="V4多周期影子账户已从各周期最新收盘K线开始",
        )

    events: list[RotationPaperEvent] = []

    def add_event(
        timestamp: pd.Timestamp,
        title: str,
        interval: str,
        *lines: str,
        severity: str = "INFO",
    ) -> None:
        """生成包含策略周期的唯一邮件事件。"""
        events.append(
            RotationPaperEvent(
                event_key=(
                    f"rotation-v4:{timestamp.isoformat()}:{interval}:{title}:{len(events)}"
                ),
                title=title,
                lines=(
                    f"北京时间：{timestamp.tz_convert('Asia/Shanghai')}",
                    f"交易周期：{interval}",
                    *lines,
                ),
                severity=severity,
            )
        )

    def open_position(
        side: str,
        interval: str,
        reference: float,
        timestamp: pd.Timestamp,
        reason: str,
    ) -> None:
        """按全账户0.25%风险建立唯一仓位，并记录该仓位所属周期。"""
        parameters = parameters_for_same_timeframe(interval)
        state.position_side = side
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
        adverse_loss = (
            reference - stop_fill if side == "LONG" else stop_fill - reference
        )
        loss_per_unit = (
            adverse_loss
            + reference * maker_fee_rate
            + stop_fill * taker_fee_rate
        )
        state.quantity = state.equity * risk_per_trade / loss_per_unit
        state.remaining_quantity = state.quantity
        state.entry_fee_remaining = reference * state.quantity * maker_fee_rate
        state.equity -= state.entry_fee_remaining
        state.trade_net_pnl = 0.0
        state.middle_reduced = False
        add_event(
            timestamp,
            f"模拟开仓：{'买入做多' if side == 'LONG' else '卖出做空'}",
            interval,
            f"原因：{reason}",
            f"轨道限价：{reference:.2f}",
            f"数量：{state.quantity:.4f} ETH",
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
        allocated_entry_fee = state.entry_fee_remaining * (
            quantity / state.remaining_quantity
        )
        exit_fee = fill * quantity * fee_rate
        net = gross - allocated_entry_fee - exit_fee
        state.equity += gross - exit_fee
        state.trade_net_pnl += net
        state.entry_fee_remaining -= allocated_entry_fee
        state.remaining_quantity -= quantity
        add_event(
            timestamp,
            f"模拟{'减仓' if state.remaining_quantity > 1e-12 else '平仓'}：{reason}",
            interval,
            f"方向：{state.position_side}",
            f"成交参考价：{fill:.2f}",
            f"本次净盈亏：{net:+.2f} USDT",
            f"整笔累计净盈亏：{state.trade_net_pnl:+.2f} USDT",
            f"影子权益：{state.equity:.2f} USDT",
            severity="WARNING" if net < 0 else "INFO",
        )

    # 把四个周期的新K线按实际收盘时刻分组；相同时刻按5m到1h顺序处理。
    grouped: dict[pd.Timestamp, list[tuple[str, int]]] = {}
    for interval in INTERVAL_PRIORITY:
        bars = bars_by_interval[interval]
        last_time = pd.Timestamp(state.last_bar_times[interval])
        if last_time < bars.index[0] - INTERVAL_DURATION[interval]:
            raise RuntimeError(f"{interval} paper history gap exceeds loaded window")
        for position, open_time in enumerate(bars.index):
            if open_time <= last_time:
                continue
            close_time = pd.Timestamp(open_time) + INTERVAL_DURATION[interval]
            grouped.setdefault(close_time, []).append((interval, position))

    processed_bars = 0
    for close_time in sorted(grouped):
        batch = sorted(
            grouped[close_time], key=lambda item: INTERVAL_PRIORITY.index(item[0])
        )
        beijing_day = str(close_time.tz_convert("Asia/Shanghai").date())
        if state.current_day != beijing_day:
            state.current_day = beijing_day
            state.day_start_equity = state.equity
            state.daily_blocked = False

        # 先更新本批次各周期箱体状态，5m止损暂停不会阻塞其他周期。
        for interval, position in batch:
            if position < 1:
                continue
            previous = contexts[interval].iloc[position - 1]
            if bool(previous["breakout"]):
                state.box_active[interval] = False
                state.blocked_after_stop[interval] = True
                state.reset_streak[interval] = 0
            if state.blocked_after_stop[interval] and not state.permanent_fuse:
                state.reset_streak[interval] = (
                    state.reset_streak[interval] + 1
                    if bool(previous["box_candidate"])
                    else 0
                )
                if state.reset_streak[interval] >= 3:
                    state.blocked_after_stop[interval] = False
                    state.box_active[interval] = True
            elif bool(previous["box_candidate"]) and not state.permanent_fuse:
                state.box_active[interval] = True

        # 已有仓位只由其开仓周期管理，其他周期不能同时开相反仓位。
        active_item = next(
            (item for item in batch if item[0] == state.active_interval), None
        )
        if state.position_side and active_item is not None:
            interval, position = active_item
            bars = bars_by_interval[interval]
            bar = bars.iloc[position]
            previous = contexts[interval].iloc[position - 1]
            upper = float(previous["bb_upper"])
            middle = float(previous["bb_middle"])
            lower = float(previous["bb_lower"])
            stop_hit = (
                state.position_side == "LONG" and float(bar["low"]) <= state.stop_price
            ) or (
                state.position_side == "SHORT" and float(bar["high"]) >= state.stop_price
            )
            target_hit = (
                state.position_side == "LONG" and float(bar["high"]) >= upper
            ) or (
                state.position_side == "SHORT" and float(bar["low"]) <= lower
            )
            if stop_hit:
                stop_reason = (
                    "中轨减仓后的保护止损"
                    if state.middle_reduced
                    else "固定5点止损"
                )
                close_quantity(
                    state.remaining_quantity,
                    state.stop_price,
                    pd.Timestamp(bars.index[position]),
                    stop_reason,
                    market=True,
                )
                stopped_interval = state.active_interval
                state.position_side = ""
                state.active_interval = ""
                state.blocked_after_stop[stopped_interval] = True
                state.box_active[stopped_interval] = False
                state.reset_streak[stopped_interval] = 0
            else:
                middle_hit = (
                    state.position_side == "LONG" and float(bar["high"]) >= middle
                ) or (
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
                    state.position_side = ""
                    if (
                        state.box_active[interval]
                        and not state.blocked_after_stop[interval]
                        and not state.daily_blocked
                        and not state.permanent_fuse
                    ):
                        new_side = "SHORT" if old_side == "LONG" else "LONG"
                        open_position(
                            new_side,
                            interval,
                            target,
                            pd.Timestamp(bars.index[position]),
                            "对侧轨止盈后同周期立即反手",
                        )
                        immediate_stop = (
                            new_side == "LONG" and float(bar["low"]) <= state.stop_price
                        ) or (
                            new_side == "SHORT" and float(bar["high"]) >= state.stop_price
                        )
                        if immediate_stop:
                            close_quantity(
                                state.remaining_quantity,
                                state.stop_price,
                                pd.Timestamp(bars.index[position]),
                                "反手后同根K线止损",
                                market=True,
                            )
                            state.position_side = ""
                            state.active_interval = ""
                            state.blocked_after_stop[interval] = True
                            state.box_active[interval] = False
                            state.reset_streak[interval] = 0
                    else:
                        state.active_interval = ""

        # 空仓时按固定优先级选本批次中第一个有效周期，绝不多空双开。
        if (
            not state.position_side
            and not state.daily_blocked
            and not state.permanent_fuse
        ):
            selected_item = next(
                (
                    (interval, position)
                    for interval, position in batch
                    if position >= 1
                    and state.box_active[interval]
                    and not state.blocked_after_stop[interval]
                ),
                None,
            )
            if selected_item is not None:
                interval, position = selected_item
                bars = bars_by_interval[interval]
                bar = bars.iloc[position]
                previous = contexts[interval].iloc[position - 1]
                upper = float(previous["bb_upper"])
                lower = float(previous["bb_lower"])
                touched_upper = float(bar["high"]) >= upper
                touched_lower = float(bar["low"]) <= lower
                if touched_upper != touched_lower:
                    side = "SHORT" if touched_upper else "LONG"
                    reference = upper if touched_upper else lower
                    open_position(
                        side,
                        interval,
                        reference,
                        pd.Timestamp(bars.index[position]),
                        f"按5m→15m→30m→1h优先级选中{interval}箱体并触轨",
                    )
                    immediate_stop = (
                        side == "LONG" and float(bar["low"]) <= state.stop_price
                    ) or (
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
                        state.active_interval = ""
                        state.blocked_after_stop[interval] = True
                        state.box_active[interval] = False
                        state.reset_streak[interval] = 0

        if state.equity / state.day_start_equity - 1.0 <= -0.02:
            if not state.daily_blocked:
                add_event(
                    close_time,
                    "模拟账户触发每日亏损熔断",
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
                state.active_interval or "ACCOUNT",
                f"当前回撤：{drawdown:.2%}",
                severity="CRITICAL",
            )
        for interval, position in batch:
            state.last_bar_times[interval] = bars_by_interval[interval].index[
                position
            ].isoformat()
            processed_bars += 1

    _save_state(state_path, state)
    selected_interval = next(
        (
            interval
            for interval in INTERVAL_PRIORITY
            if state.box_active[interval] and not state.blocked_after_stop[interval]
        ),
        "",
    )
    return MultiTimeframePaperSummary(
        status="FUSED" if state.permanent_fuse else "RUNNING",
        processed_bars=processed_bars,
        events=tuple(events),
        equity=state.equity,
        position_side=state.position_side,
        active_interval=state.active_interval,
        selected_interval=selected_interval,
        reason="V4多周期单持仓影子模拟；Gate订单提交接口未调用",
    )
