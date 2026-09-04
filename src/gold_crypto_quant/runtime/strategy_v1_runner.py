"""震荡 v1.0 与顺势 v1.0 的双策略对照服务。

取代原来的 Gate/币安双交易所对照——对照轴从"交易所"换成了"策略"：
同一份 Gate 行情喂给两个策略，各自独立账户，看谁做得好。

节奏：策略按已收线 K 线决策。执行周期最小可到 1m，所以默认 5 秒查一次游标，
有新收线 K 线才真正跑策略——比 V5.8 的 1 秒轮询省，又不会漏掉 1m 的收线。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from gold_crypto_quant.config import get_settings
from gold_crypto_quant.market_data.gate_history import GATE_LIVE_VENUE
from gold_crypto_quant.runtime.macro_blackout import active_blackout
from gold_crypto_quant.storage.bar_source import check_health, load_bars
from gold_crypto_quant.strategies_v1 import live as live_module
from gold_crypto_quant.strategies_v1.params import load_config

INTERVALS = ("1m", "5m", "15m", "30m", "1h")
DEFAULT_SYMBOLS = ("BTC_USDT", "ETH_USDT")


@dataclass(slots=True)
class CycleReport:
    cycle: int
    advanced: bool
    events: int
    equities: dict[str, float]
    reason: str


def _log(message: str) -> None:
    print(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}] {message}", flush=True)


class StrategyComparisonRunner:
    """两个策略共用一次取数，各自推进各自的账户。"""

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
        venue: str = GATE_LIVE_VENUE,
        poll_seconds: float = 5.0,
        bar_limit: int = 400,
        log_every_cycle: bool = False,
    ) -> None:
        self.symbols = symbols
        self.venue = venue
        self.poll_seconds = poll_seconds
        self.bar_limit = bar_limit
        self.log_every_cycle = log_every_cycle
        self.config = load_config()
        self.range_live, self.trend_live = live_module.build(self.config)
        self.cycle = 0

    # ------------------------------------------------------------------

    def run_once(self) -> CycleReport:
        self.cycle += 1
        health = check_health(self.venue)
        # 宏观数据窗口只挡开仓；已有仓位的止损止盈照常执行，所以不是整轮跳过，
        # 而是这一轮不喂新 K 线给还没持仓的策略。
        settings = get_settings()
        blackout = None
        if settings.macro_blackout_before_minutes or settings.macro_blackout_after_minutes:
            blackout = active_blackout(
                datetime.now(UTC),
                before=settings.macro_blackout_before_minutes,
                after=settings.macro_blackout_after_minutes,
            )

        total_events = 0
        advanced = False
        for symbol in self.symbols:
            bars = {
                interval: load_bars(
                    symbol, interval, venue=self.venue, limit=self.bar_limit, health=health
                )
                for interval in INTERVALS
            }
            bars = {k: v for k, v in bars.items() if v is not None and not v.empty}
            if not bars:
                continue
            for entry in (self.range_live, self.trend_live):
                if blackout is not None and not entry.account.positions.get(symbol):
                    continue
                produced = live_module.advance(entry, symbol, bars)
                if produced:
                    advanced = True
                    total_events += len(produced)
                    for event in produced:
                        _log(
                            f"[{entry.strategy.name}] {event.kind} {symbol} {event.side} "
                            f"@{event.price:.2f} × {event.quantity:.6f}"
                            + (f" 盈亏{event.pnl:+.2f}U" if event.kind == "平仓" else "")
                            + f"；{event.reason}"
                        )

        for entry in (self.range_live, self.trend_live):
            live_module.persist(entry)

        equities = {
            self.range_live.strategy.name: self.range_live.account.equity,
            self.trend_live.strategy.name: self.trend_live.account.equity,
        }
        reason = "宏观静默中，仅维护已有仓位" if blackout else "正常"
        report = CycleReport(self.cycle, advanced, total_events, equities, reason)
        if self.log_every_cycle or advanced:
            gap = equities["震荡"] - equities["顺势"]
            _log(
                f"第{self.cycle}轮：震荡 {equities['震荡']:.2f}U"
                f"（{self.range_live.account.trade_count}笔）、"
                f"顺势 {equities['顺势']:.2f}U"
                f"（{self.trend_live.account.trade_count}笔）；差 {gap:+.2f}U；{reason}"
            )
        return report

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        _log(
            f"策略对照服务启动：{self.range_live.strategy.name} v"
            f"{self.range_live.strategy.version} vs {self.trend_live.strategy.name} v"
            f"{self.trend_live.strategy.version}；行情 {self.venue}，"
            f"品种 {'、'.join(self.symbols)}；真实交易始终关闭"
        )
        while max_cycles is None or self.cycle < max_cycles:
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - 单轮失败不能带停服务
                _log(f"本轮失败：{type(error).__name__}: {error}")
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self.poll_seconds - elapsed))


def run_strategy_comparison(
    *,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    poll_seconds: float = 5.0,
    max_cycles: int | None = None,
    log_every_cycle: bool = False,
) -> None:
    runner = StrategyComparisonRunner(
        symbols=symbols, poll_seconds=poll_seconds, log_every_cycle=log_every_cycle
    )
    runner.run_forever(max_cycles=max_cycles)
