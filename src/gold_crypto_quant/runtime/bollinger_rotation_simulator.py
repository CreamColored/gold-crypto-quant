"""ETH 15分钟布林带轨道轮转的纯本地影子模拟器。"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from gold_crypto_quant.strategy.bollinger_range import (
    BollingerRangeParameters,
    build_rotation_box_context,
    parameters_for_same_timeframe,
)

ROTATION_STRATEGY_VERSION = "3.0.0"
DEFAULT_STATE_PATH = Path(".runtime/bollinger-rotation-paper-v3.json")


@dataclass(slots=True)
class RotationPaperState:
    """跨轮询持久化的影子资金、持仓和箱体状态。"""

    strategy_version: str = ROTATION_STRATEGY_VERSION
    equity: float = 10_000.0
    peak_equity: float = 10_000.0
    day_start_equity: float = 10_000.0
    current_day: str = ""
    last_bar_time: str = ""
    position_side: str = ""
    quantity: float = 0.0
    remaining_quantity: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    entry_fee_remaining: float = 0.0
    trade_net_pnl: float = 0.0
    middle_reduced: bool = False
    box_active: bool = False
    blocked_after_stop: bool = False
    reset_streak: int = 0
    daily_blocked: bool = False
    permanent_fuse: bool = False


@dataclass(frozen=True, slots=True)
class RotationPaperEvent:
    """交给邮件通知层的单笔模拟成交或特殊状态。"""

    event_key: str
    title: str
    lines: tuple[str, ...]
    severity: str = "INFO"


@dataclass(frozen=True, slots=True)
class RotationPaperSummary:
    """一轮增量模拟的处理摘要。"""

    status: str
    processed_bars: int
    events: tuple[RotationPaperEvent, ...]
    equity: float
    position_side: str
    reason: str


def _load_state(path: Path) -> RotationPaperState | None:
    """读取本地状态；不存在时由调用方执行首次初始化。"""
    if not path.exists():
        return None
    values = json.loads(path.read_text(encoding="utf-8"))
    state = RotationPaperState(**values)
    if state.strategy_version != ROTATION_STRATEGY_VERSION:
        raise RuntimeError("rotation paper state strategy version mismatch")
    return state


def _save_state(path: Path, state: RotationPaperState) -> None:
    """原子替换状态文件，避免Mac异常关机留下半截JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def run_rotation_paper_cycle(
    bars: pd.DataFrame,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    initial_equity: float = 10_000.0,
    parameters: BollingerRangeParameters | None = None,
    maker_fee_rate: float = -0.0001,
    taker_fee_rate: float = 0.00075,
    stop_slippage_rate: float = 0.0002,
    risk_per_trade: float = 0.0025,
) -> RotationPaperSummary:
    """逐根补算新收盘K线，并持久化完全不连接交易所订单接口的影子成交。"""
    if len(bars) < 30:
        raise ValueError("rotation paper simulator requires at least 30 bars")
    parameters = parameters or parameters_for_same_timeframe("15m")
    context = build_rotation_box_context(bars, parameters)
    state = _load_state(state_path)
    if state is None:
        state = RotationPaperState(
            equity=initial_equity,
            peak_equity=initial_equity,
            day_start_equity=initial_equity,
            last_bar_time=bars.index[-1].isoformat(),
        )
        # 首次启动只建立观察游标，禁止把历史触轨伪装成刚刚发生的模拟成交。
        _save_state(state_path, state)
        return RotationPaperSummary(
            status="INITIALIZED",
            processed_bars=0,
            events=(),
            equity=state.equity,
            position_side="",
            reason="影子账户已从最新收盘K线开始，未回放历史订单",
        )

    last_time = pd.Timestamp(state.last_bar_time)
    expected_interval = pd.Timedelta(minutes=15)
    if last_time < bars.index[0] - expected_interval:
        raise RuntimeError("rotation paper history gap exceeds loaded 15m window")
    new_positions = [
        position for position, timestamp in enumerate(bars.index) if timestamp > last_time
    ]
    events: list[RotationPaperEvent] = []

    def event(timestamp: pd.Timestamp, title: str, *lines: str, severity: str = "INFO") -> None:
        """加入带时间与唯一键的事件，通知层可以幂等发送邮件。"""
        events.append(
            RotationPaperEvent(
                event_key=f"rotation-v3:{timestamp.isoformat()}:{title}:{len(events)}",
                title=title,
                lines=(f"北京时间：{timestamp.tz_convert('Asia/Shanghai')}", *lines),
                severity=severity,
            )
        )

    def open_position(side: str, reference: float, timestamp: pd.Timestamp, reason: str) -> None:
        """按轨道限价和包含止损成本的0.25%风险预算建立影子仓位。"""
        state.position_side = side
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
        event(
            timestamp,
            f"模拟开仓：{'买入做多' if side == 'LONG' else '卖出做空'}",
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
        """执行模拟减仓或平仓，并把手续费、滑点和盈亏立即计入影子权益。"""
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
        event(
            timestamp,
            f"模拟{'减仓' if state.remaining_quantity > 1e-12 else '平仓'}：{reason}",
            f"方向：{state.position_side}",
            f"成交参考价：{fill:.2f}",
            f"本次净盈亏：{net:+.2f} USDT",
            f"整笔累计净盈亏：{state.trade_net_pnl:+.2f} USDT",
            f"影子权益：{state.equity:.2f} USDT",
            severity="WARNING" if net < 0 else "INFO",
        )

    for position in new_positions:
        if position < 1:
            continue
        timestamp = pd.Timestamp(bars.index[position])
        bar = bars.iloc[position]
        previous = context.iloc[position - 1]
        beijing_day = str(timestamp.tz_convert("Asia/Shanghai").date())
        if state.current_day != beijing_day:
            state.current_day = beijing_day
            state.day_start_equity = state.equity
            state.daily_blocked = False

        if bool(previous["breakout"]):
            state.box_active = False
            state.blocked_after_stop = True
            state.reset_streak = 0
        if state.blocked_after_stop and not state.permanent_fuse:
            state.reset_streak = (
                state.reset_streak + 1 if bool(previous["box_candidate"]) else 0
            )
            if state.reset_streak >= 3:
                state.blocked_after_stop = False
                state.box_active = True
        elif bool(previous["box_candidate"]) and not state.permanent_fuse:
            state.box_active = True

        upper = float(previous["bb_upper"])
        middle = float(previous["bb_middle"])
        lower = float(previous["bb_lower"])
        if state.position_side:
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
                close_quantity(
                    state.remaining_quantity,
                    state.stop_price,
                    timestamp,
                    "固定5点止损",
                    market=True,
                )
                state.position_side = ""
                state.blocked_after_stop = True
                state.box_active = False
                state.reset_streak = 0
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
                        timestamp,
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
                        timestamp,
                        "到达对侧轨止盈",
                        market=False,
                    )
                    state.position_side = ""
                    if (
                        state.box_active
                        and not state.blocked_after_stop
                        and not state.daily_blocked
                        and not state.permanent_fuse
                    ):
                        new_side = "SHORT" if old_side == "LONG" else "LONG"
                        open_position(new_side, target, timestamp, "对侧轨止盈后立即反手")
                        immediate_stop = (
                            new_side == "LONG" and float(bar["low"]) <= state.stop_price
                        ) or (
                            new_side == "SHORT" and float(bar["high"]) >= state.stop_price
                        )
                        if immediate_stop:
                            close_quantity(
                                state.remaining_quantity,
                                state.stop_price,
                                timestamp,
                                "反手后同根K线止损",
                                market=True,
                            )
                            state.position_side = ""
                            state.blocked_after_stop = True
                            state.box_active = False
                            state.reset_streak = 0

        if (
            not state.position_side
            and state.box_active
            and not state.blocked_after_stop
            and not state.daily_blocked
            and not state.permanent_fuse
        ):
            touched_upper = float(bar["high"]) >= upper
            touched_lower = float(bar["low"]) <= lower
            # 同根同时穿越两端时无法确定先后顺序，保守跳过而不是挑选有利方向。
            if touched_upper != touched_lower:
                side = "SHORT" if touched_upper else "LONG"
                reference = upper if touched_upper else lower
                open_position(side, reference, timestamp, "箱体内触及布林轨")
                immediate_stop = (
                    side == "LONG" and float(bar["low"]) <= state.stop_price
                ) or (
                    side == "SHORT" and float(bar["high"]) >= state.stop_price
                )
                if immediate_stop:
                    close_quantity(
                        state.remaining_quantity,
                        state.stop_price,
                        timestamp,
                        "触轨开仓后同根K线止损",
                        market=True,
                    )
                    state.position_side = ""
                    state.blocked_after_stop = True
                    state.box_active = False
                    state.reset_streak = 0

        if state.equity / state.day_start_equity - 1.0 <= -0.02:
            if not state.daily_blocked:
                event(
                    timestamp,
                    "模拟账户触发每日亏损熔断",
                    f"当日权益变化：{state.equity / state.day_start_equity - 1.0:.2%}",
                    severity="CRITICAL",
                )
            state.daily_blocked = True
        state.peak_equity = max(state.peak_equity, state.equity)
        drawdown = 1.0 - state.equity / state.peak_equity
        if drawdown >= 0.08 and not state.permanent_fuse:
            state.permanent_fuse = True
            state.box_active = False
            event(
                timestamp,
                "模拟账户触发8%最大回撤熔断",
                f"当前回撤：{drawdown:.2%}",
                severity="CRITICAL",
            )
        state.last_bar_time = timestamp.isoformat()

    _save_state(state_path, state)
    return RotationPaperSummary(
        status="FUSED" if state.permanent_fuse else "RUNNING",
        processed_bars=len(new_positions),
        events=tuple(events),
        equity=state.equity,
        position_side=state.position_side,
        reason="本地影子模拟；Gate订单提交接口未调用",
    )
