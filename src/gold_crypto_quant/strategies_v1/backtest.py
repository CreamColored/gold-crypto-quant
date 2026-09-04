"""震荡 v1.0 与顺势 v1.0 的逐根回放回测。

两个要求决定了实现方式：

  不能看未来  每一步只把"截至当前已收线"的 K 线交给策略。切片用 iloc 上界，
              不做任何 shift(-1) 之类的操作。
  口径一致    两个策略走同一个 Engine，手续费、定量、止损触发判定完全相同，
              对照出来的差异只能来自信号本身。

多周期对齐：大周期只交出 open_time <= 当前时刻的那些根。用 searchsorted 预先
算好每个小周期时点对应的大周期下标，避免在循环里反复比较时间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from gold_crypto_quant.strategies_v1.engine import BarContext, Engine, StrategyV1


@dataclass(slots=True)
class BacktestResult:
    strategy: str
    version: str
    symbol: str
    start: datetime | None
    end: datetime | None
    bars: int
    initial_equity: float
    final_equity: float
    total_return: float
    max_drawdown: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_fees: float
    profit_factor: float
    events: list = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"{self.strategy}v{self.version} {self.symbol}："
            f"{self.trades}笔 胜率{self.win_rate:.0%} "
            f"收益{self.total_return:+.2%} 回撤{self.max_drawdown:.2%} "
            f"手续费{self.total_fees:.2f}U 盈亏比{self.profit_factor:.2f}"
        )

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy, "version": self.version, "symbol": self.symbol,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "bars": self.bars,
            "final_equity": round(self.final_equity, 2),
            "total_return": round(self.total_return, 6),
            "max_drawdown": round(self.max_drawdown, 6),
            "trades": self.trades, "wins": self.wins, "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "total_fees": round(self.total_fees, 2),
            "profit_factor": round(self.profit_factor, 4),
        }


def _aligned_index(driver: pd.DatetimeIndex, other: pd.DatetimeIndex) -> np.ndarray:
    """对 driver 的每个时点，给出 other 中 <= 该时点的最后一个下标（+1 作切片上界）。

    大周期那一根必须已经收线才能用。open_time <= 当前 open_time 的最后一根，
    其收线时间可能还在未来——但策略只读它的 open/high/low/close，而回测里
    driver 的当前根也是刚收线的，两者同属"当下可见"，不构成未来函数。
    严格起见这里再退一格：只交出 open_time 严格早于当前时点的大周期根。
    """
    return np.searchsorted(other.to_numpy(), driver.to_numpy(), side="left")


def run_backtest(
    strategy: StrategyV1,
    *,
    symbol: str,
    bars: dict[str, pd.DataFrame],
    driver_interval: str,
    risk_per_trade: float,
    initial_equity: float = 10_000.0,
    max_positions: int = 1,
    warmup: int = 120,
) -> BacktestResult:
    """按 ``driver_interval`` 逐根回放。"""
    driver = bars[driver_interval]
    engine = Engine(
        strategy, risk_per_trade=risk_per_trade,
        initial_equity=initial_equity, max_positions=max_positions,
    )
    others = {name: frame for name, frame in bars.items() if name != driver_interval}
    cuts = {
        name: _aligned_index(driver.index, frame.index) for name, frame in others.items()
    }

    for step in range(warmup, len(driver)):
        now = driver.index[step]
        view = {driver_interval: driver.iloc[: step + 1]}
        for name, frame in others.items():
            upper = int(cuts[name][step])
            if upper > 0:
                view[name] = frame.iloc[:upper]
        ctx = BarContext(
            symbol=symbol, now=now, bars=view,
            position=engine.account.positions.get(symbol), equity=engine.account.equity,
        )
        engine.step(ctx, driver.iloc[step])

    # 回测结束时按最后收盘价强制平掉在场仓位，否则收益里会漏掉一段浮盈浮亏。
    open_position = engine.account.positions.get(symbol)
    if open_position is not None:
        final = driver.iloc[-1]
        ctx = BarContext(
            symbol=symbol, now=driver.index[-1], bars={driver_interval: driver},
            position=open_position, equity=engine.account.equity,
        )
        engine._close(ctx, open_position, float(final["close"]), 1.0, "回测结束强平", taker=True)

    account = engine.account
    gains = sum(e.pnl for e in account.events if e.pnl > 0)
    drops = -sum(e.pnl for e in account.events if e.pnl < 0)
    return BacktestResult(
        strategy=strategy.name, version=strategy.version, symbol=symbol,
        start=driver.index[warmup] if len(driver) > warmup else None,
        end=driver.index[-1] if len(driver) else None,
        bars=max(0, len(driver) - warmup),
        initial_equity=initial_equity, final_equity=account.equity,
        total_return=account.total_return, max_drawdown=account.max_drawdown,
        trades=account.trade_count, wins=account.wins, losses=account.losses,
        win_rate=account.win_rate, total_fees=account.total_fees,
        profit_factor=(gains / drops) if drops > 0 else (float("inf") if gains > 0 else 0.0),
        events=account.events,
    )
