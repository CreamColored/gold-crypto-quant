"""震荡 v1.0 与顺势 v1.0 共用的执行引擎。

两个策略只回答"要不要动、往哪个方向动、止损放哪"，成交价、手续费、仓位大小、
止损止盈的触发判定全部由这里统一处理。对照实验的公平性靠的就是这一点——
两边的差异只能来自信号，不能来自账务口径。

评估时点是**已收线 K 线**，不是分钟内触价。这与课程一致：L8R10「实体收盘」、
L8R17「实体有效收盘回到箱内」、L9R6、L10R4 都要求收盘确认。V5.8 那套在途触轨
＋停留确认的机制在这里不需要。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

import pandas as pd

Side = Literal["LONG", "SHORT"]

# 与 V5.8 同口径：币安/Gate U 本位永续 VIP0 公开费率。
# 不要改成负的 maker 返佣，那是 VIP4 以上才有的。
MAKER_FEE_RATE = 0.0002
TAKER_FEE_RATE = 0.0005
STOP_SLIPPAGE_RATE = 0.0002


# ---------------------------------------------------------------- 策略产出的意图


@dataclass(frozen=True, slots=True)
class OpenIntent:
    side: Side
    stop_price: float
    take_profit: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CloseIntent:
    fraction: float
    reason: str = ""


@dataclass(frozen=True, slots=True)
class MoveStopIntent:
    stop_price: float
    reason: str = ""


Intent = OpenIntent | CloseIntent | MoveStopIntent


# ---------------------------------------------------------------- 账户与仓位


@dataclass(slots=True)
class Position:
    side: Side
    quantity: float
    remaining: float
    entry_price: float
    stop_price: float
    take_profit: float
    entry_time: datetime
    entry_fee_remaining: float
    realized: float = 0.0
    peak_price: float = 0.0
    trough_price: float = 0.0
    reason: str = ""

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)


@dataclass(slots=True)
class TradeEvent:
    time: datetime
    symbol: str
    kind: str
    side: str
    price: float
    quantity: float
    pnl: float
    equity: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "time": self.time.isoformat(),
            "symbol": self.symbol,
            "kind": self.kind,
            "side": self.side,
            "price": round(self.price, 6),
            "quantity": round(self.quantity, 8),
            "pnl": round(self.pnl, 4),
            "equity": round(self.equity, 4),
            "reason": self.reason,
        }


@dataclass(slots=True)
class Account:
    equity: float = 10_000.0
    initial_equity: float = 10_000.0
    peak_equity: float = 10_000.0
    total_fees: float = 0.0
    wins: int = 0
    losses: int = 0
    positions: dict[str, Position] = field(default_factory=dict)
    events: list[TradeEvent] = field(default_factory=list)

    @property
    def trade_count(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.trade_count if self.trade_count else 0.0

    @property
    def total_return(self) -> float:
        return self.equity / self.initial_equity - 1.0 if self.initial_equity else 0.0

    @property
    def max_drawdown(self) -> float:
        return 1.0 - self.equity / self.peak_equity if self.peak_equity else 0.0


# ---------------------------------------------------------------- 策略接口


@dataclass(frozen=True, slots=True)
class BarContext:
    """交给策略的只读视图。"""

    symbol: str
    now: datetime
    bars: dict[str, pd.DataFrame]
    """周期 → 截至当前时刻的已收线 K 线。策略不得访问未来数据。"""
    position: Position | None
    equity: float


class StrategyV1(Protocol):
    name: str
    version: str

    def on_bar(self, ctx: BarContext) -> list[Intent]: ...


# ---------------------------------------------------------------- 引擎


class Engine:
    """一个策略一个引擎实例，各自独立的账户。"""

    def __init__(
        self,
        strategy: StrategyV1,
        *,
        risk_per_trade: float,
        initial_equity: float = 10_000.0,
        maker_fee_rate: float = MAKER_FEE_RATE,
        taker_fee_rate: float = TAKER_FEE_RATE,
        stop_slippage_rate: float = STOP_SLIPPAGE_RATE,
        max_positions: int = 1,
    ) -> None:
        self.strategy = strategy
        self.risk_per_trade = risk_per_trade
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self.stop_slippage_rate = stop_slippage_rate
        self.max_positions = max_positions
        self.account = Account(equity=initial_equity, initial_equity=initial_equity,
                               peak_equity=initial_equity)

    # -------- 对外：处理一根已收线 K 线 --------

    def step(self, ctx: BarContext, bar: pd.Series) -> None:
        """先按 K 线极值判保护性出场，再让策略决策。

        顺序不能反：本根 K 线可能既打止损又给出反向信号，先执行风险动作
        才不会把一笔已经该止损的仓位带进下一步。
        """
        position = self.account.positions.get(ctx.symbol)
        if position is not None:
            self._update_extremes(position, bar)
            if self._check_protective_exit(ctx, position, bar):
                position = None

        live = BarContext(
            symbol=ctx.symbol, now=ctx.now, bars=ctx.bars,
            position=self.account.positions.get(ctx.symbol), equity=self.account.equity,
        )
        for intent in self.strategy.on_bar(live):
            self._apply(intent, live, bar)

    # -------- 内部 --------

    @staticmethod
    def _update_extremes(position: Position, bar: pd.Series) -> None:
        high, low = float(bar["high"]), float(bar["low"])
        position.peak_price = max(position.peak_price or high, high)
        position.trough_price = min(position.trough_price or low, low)

    def _check_protective_exit(self, ctx: BarContext, position: Position, bar: pd.Series) -> bool:
        """止损与固定止盈。同一根内两者都被触及时按止损处理——
        用 1 分钟以上的 K 线无法知道先后，这个假设是保守的，不会高估收益。"""
        high, low = float(bar["high"]), float(bar["low"])
        stop_hit = (position.side == "LONG" and low <= position.stop_price) or (
            position.side == "SHORT" and high >= position.stop_price
        )
        if stop_hit:
            slip = 1 - self.stop_slippage_rate if position.side == "LONG" else 1 + self.stop_slippage_rate
            self._close(ctx, position, position.stop_price * slip, 1.0, "止损", taker=True)
            return True
        if position.take_profit:
            target_hit = (position.side == "LONG" and high >= position.take_profit) or (
                position.side == "SHORT" and low <= position.take_profit
            )
            if target_hit:
                self._close(ctx, position, position.take_profit, 1.0, "止盈", taker=False)
                return True
        return False

    def _apply(self, intent: Intent, ctx: BarContext, bar: pd.Series) -> None:
        close = float(bar["close"])
        position = self.account.positions.get(ctx.symbol)
        if isinstance(intent, OpenIntent):
            if position is not None or len(self.account.positions) >= self.max_positions:
                return
            self._open(ctx, intent, close)
        elif isinstance(intent, CloseIntent) and position is not None:
            self._close(ctx, position, close, intent.fraction, intent.reason, taker=True)
        elif isinstance(intent, MoveStopIntent) and position is not None:
            position.stop_price = intent.stop_price

    def _open(self, ctx: BarContext, intent: OpenIntent, price: float) -> None:
        distance = abs(price - intent.stop_price)
        if distance <= 0:
            return
        # L13R9：开仓数量 = (账户总资金 × 单笔风险比例) ÷ 止损点数。
        quantity = self.account.equity * self.risk_per_trade / distance
        if quantity <= 0:
            return
        fee = price * quantity * self.taker_fee_rate
        self.account.equity -= fee
        self.account.total_fees += fee
        self.account.positions[ctx.symbol] = Position(
            side=intent.side, quantity=quantity, remaining=quantity, entry_price=price,
            stop_price=intent.stop_price, take_profit=intent.take_profit, entry_time=ctx.now,
            entry_fee_remaining=0.0, peak_price=price, trough_price=price, reason=intent.reason,
        )
        self._record(ctx, "开仓", intent.side, price, quantity, 0.0, intent.reason)

    def _close(
        self, ctx: BarContext, position: Position, price: float,
        fraction: float, reason: str, *, taker: bool,
    ) -> None:
        quantity = position.remaining * min(max(fraction, 0.0), 1.0)
        if quantity <= 0:
            return
        direction = 1.0 if position.side == "LONG" else -1.0
        gross = (price - position.entry_price) * direction * quantity
        fee = price * quantity * (self.taker_fee_rate if taker else self.maker_fee_rate)
        net = gross - fee
        self.account.equity += net
        self.account.total_fees += fee
        self.account.peak_equity = max(self.account.peak_equity, self.account.equity)
        position.remaining -= quantity
        position.realized += net
        if position.remaining <= position.quantity * 1e-9:
            if position.realized >= 0:
                self.account.wins += 1
            else:
                self.account.losses += 1
            self.account.positions.pop(ctx.symbol, None)
        self._record(ctx, "平仓", position.side, price, quantity, net, reason)

    def _record(
        self, ctx: BarContext, kind: str, side: str,
        price: float, quantity: float, pnl: float, reason: str,
    ) -> None:
        self.account.events.append(
            TradeEvent(time=ctx.now, symbol=ctx.symbol, kind=kind, side=side, price=price,
                       quantity=quantity, pnl=pnl, equity=self.account.equity, reason=reason)
        )
