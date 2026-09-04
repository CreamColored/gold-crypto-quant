"""震荡 v1.0 与顺势 v1.0 的实盘影子运行与状态持久化。

两个策略跑在同一份 Gate 行情上，各自独立账户，共用 Engine——
对照的差异只能来自信号，不能来自账务口径。

状态落成 JSON，Web 直接读文件，与 V5.8 时期的做法一致，不引入新的读写通道。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from gold_crypto_quant.strategies_v1.backtest import INTERVAL_DURATION
from gold_crypto_quant.strategies_v1.engine import (
    Account,
    BarContext,
    Engine,
    Position,
    TradeEvent,
)
from gold_crypto_quant.strategies_v1.params import StrategyConfig, load_config
from gold_crypto_quant.strategies_v1.range_v1 import RangeStrategyV1
from gold_crypto_quant.strategies_v1.trend_v1 import TrendStrategyV1

STATE_DIR = Path(os.environ.get("GCQ_STATE_DIR") or ".runtime")
RANGE_STATE_PATH = STATE_DIR / "strategy-range-v1.json"
TREND_STATE_PATH = STATE_DIR / "strategy-trend-v1.json"

# 伪 venue：复用既有的 venue 列把两个策略的账户、订单、成交分开，
# 不必为对照实验新增一套表结构。
RANGE_VENUE = "GATE_RANGE_V1"
TREND_VENUE = "GATE_TREND_V1"

# 只保留最近这么多条事件在状态文件里；全量历史进数据库，不进 JSON。
MAX_EVENTS = 400


@dataclass(slots=True)
class LiveStrategy:
    """一个策略 + 它的引擎 + 它的落盘状态。"""

    key: str
    venue: str
    state_path: Path
    engine: Engine
    strategy: object
    driver_interval: str
    cursor: dict[str, str]

    @property
    def account(self) -> Account:
        return self.engine.account


def build(config: StrategyConfig | None = None) -> tuple[LiveStrategy, LiveStrategy]:
    """建出两个策略实例，并从磁盘恢复各自的状态。"""
    config = config or load_config()
    pair = []
    for key, venue, path, factory, params, driver in (
        ("range", RANGE_VENUE, RANGE_STATE_PATH, RangeStrategyV1, config.range_params,
         config.range_params.execution_interval),
        ("trend", TREND_VENUE, TREND_STATE_PATH, TrendStrategyV1, config.trend_params,
         config.trend_params.execution_interval),
    ):
        strategy = factory(params)
        engine = Engine(
            strategy,
            risk_per_trade=params.risk_per_trade,
            max_positions=params.max_positions,
        )
        live = LiveStrategy(
            key=key, venue=venue, state_path=path, engine=engine,
            strategy=strategy, driver_interval=driver, cursor={},
        )
        restore(live)
        pair.append(live)
    return pair[0], pair[1]


# ---------------------------------------------------------------- 持久化


def snapshot(live: LiveStrategy) -> dict:
    account = live.account
    return {
        "key": live.key,
        "venue": live.venue,
        "strategy": live.strategy.name,
        "version": live.strategy.version,
        "driver_interval": live.driver_interval,
        "updated_at": datetime.now(UTC).isoformat(),
        "equity": account.equity,
        "initial_equity": account.initial_equity,
        "peak_equity": account.peak_equity,
        "total_fees": account.total_fees,
        "wins": account.wins,
        "losses": account.losses,
        "cursor": live.cursor,
        "positions": {
            symbol: {
                "side": p.side, "quantity": p.quantity, "remaining": p.remaining,
                "entry_price": p.entry_price, "stop_price": p.stop_price,
                "take_profit": p.take_profit, "entry_time": p.entry_time.isoformat(),
                "entry_fee_remaining": p.entry_fee_remaining, "realized": p.realized,
                "peak_price": p.peak_price, "trough_price": p.trough_price,
                "reason": p.reason,
            }
            for symbol, p in account.positions.items()
        },
        "events": [e.to_dict() for e in account.events[-MAX_EVENTS:]],
        "params": live.strategy.params.to_dict(),
    }


def persist(live: LiveStrategy) -> None:
    """原子写：先落临时文件再 rename，避免 Web 读到写了一半的 JSON。"""
    live.state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = live.state_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(snapshot(live), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(live.state_path)


def restore(live: LiveStrategy) -> None:
    if not live.state_path.exists():
        return
    data = json.loads(live.state_path.read_text(encoding="utf-8"))
    account = live.account
    account.equity = float(data.get("equity", account.equity))
    account.initial_equity = float(data.get("initial_equity", account.initial_equity))
    account.peak_equity = float(data.get("peak_equity", account.peak_equity))
    account.total_fees = float(data.get("total_fees", 0.0))
    account.wins = int(data.get("wins", 0))
    account.losses = int(data.get("losses", 0))
    live.cursor = dict(data.get("cursor", {}))
    for symbol, raw in (data.get("positions") or {}).items():
        account.positions[symbol] = Position(
            side=raw["side"], quantity=raw["quantity"], remaining=raw["remaining"],
            entry_price=raw["entry_price"], stop_price=raw["stop_price"],
            take_profit=raw.get("take_profit", 0.0),
            entry_time=datetime.fromisoformat(raw["entry_time"]),
            entry_fee_remaining=raw.get("entry_fee_remaining", 0.0),
            realized=raw.get("realized", 0.0), peak_price=raw.get("peak_price", 0.0),
            trough_price=raw.get("trough_price", 0.0), reason=raw.get("reason", ""),
        )
    for raw in data.get("events", []):
        account.events.append(
            TradeEvent(
                time=datetime.fromisoformat(raw["time"]), symbol=raw["symbol"],
                kind=raw["kind"], side=raw["side"], price=raw["price"],
                quantity=raw["quantity"], pnl=raw["pnl"], equity=raw["equity"],
                reason=raw["reason"],
            )
        )


# ---------------------------------------------------------------- 单轮推进


def advance(
    live: LiveStrategy, symbol: str, bars: dict[str, pd.DataFrame]
) -> list[TradeEvent]:
    """把该品种上所有尚未处理的已收线 K 线补齐处理。

    用游标而不是"只处理最后一根"：服务重启或某一轮卡住时，
    中间跨过的 K 线必须补上，否则策略会漏掉那几根上的判定。
    """
    driver = bars.get(live.driver_interval)
    if driver is None or driver.empty:
        return []
    seen = live.cursor.get(symbol)
    if seen is None:
        # 冷启动：把游标直接对到最后一根，不回放历史。
        # 回放会把历史回测的成交记成实盘成绩，两者混在一起就没法比了；
        # 要看历史表现请用 scripts/backtest_v1.py。
        live.cursor[symbol] = driver.index[-1].isoformat()
        return []
    last_seen = pd.Timestamp(seen)
    before = len(live.account.events)

    for step in range(len(driver)):
        stamp = driver.index[step]
        if last_seen is not None and stamp <= last_seen:
            continue
        view = {live.driver_interval: driver.iloc[: step + 1]}
        for name, frame in bars.items():
            if name == live.driver_interval or frame.empty:
                continue
            # 同 backtest._aligned_index：判据是收线时间，不是 open_time。
            upper = int((frame.index + INTERVAL_DURATION[name]).searchsorted(stamp, side="right"))
            if upper > 0:
                view[name] = frame.iloc[:upper]
        ctx = BarContext(
            symbol=symbol, now=stamp.to_pydatetime(), bars=view,
            position=live.account.positions.get(symbol), equity=live.account.equity,
        )
        live.engine.step(ctx, driver.iloc[step])
        live.cursor[symbol] = stamp.isoformat()

    if len(live.account.events) > MAX_EVENTS * 2:
        del live.account.events[:-MAX_EVENTS]
    return live.account.events[before:]


def describe(live: LiveStrategy, symbols: tuple[str, ...]) -> dict:
    """给 Web 的对照视图。"""
    account = live.account
    return {
        "key": live.key,
        "strategy": live.strategy.name,
        "version": live.strategy.version,
        "venue": live.venue,
        "equity": round(account.equity, 2),
        "initial_equity": round(account.initial_equity, 2),
        "pnl": round(account.equity - account.initial_equity, 2),
        "total_return": round(account.total_return, 6),
        "max_drawdown": round(account.max_drawdown, 6),
        "trades": account.trade_count,
        "wins": account.wins,
        "losses": account.losses,
        "win_rate": round(account.win_rate, 4),
        "total_fees": round(account.total_fees, 2),
        "holdings": [
            {
                "symbol": symbol, "side": p.side, "entry": round(p.entry_price, 4),
                "stop": round(p.stop_price, 4), "target": round(p.take_profit, 4),
                "remaining": round(p.remaining, 8),
            }
            for symbol, p in account.positions.items()
        ],
        "judgement": {s: live.strategy.describe(s) for s in symbols},
        "recent": [e.to_dict() for e in account.events[-20:]][::-1],
    }
