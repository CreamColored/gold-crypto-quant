"""15分钟布林带轨道轮转的本地影子模拟周期。"""

from dataclasses import dataclass
from pathlib import Path

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.runtime.bollinger_rotation_simulator import RotationPaperEvent
from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    run_multi_timeframe_paper_cycle,
)
from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.market_health import refresh_market_health


@dataclass(frozen=True, slots=True)
class BollingerSignalCycleSummary:
    """本轮新策略观察结果。"""

    status: str
    new_signal_count: int
    order_count: int
    reason: str
    paper_status: str = "NOT_STARTED"
    paper_equity: float = 0.0
    paper_events: tuple[RotationPaperEvent, ...] = ()
    # 当前在场仓位摘要，直接进通知；无持仓时为"全部空仓"。
    paper_holdings: str = ""


def run_bollinger_signal_cycle(
    *,
    symbols: tuple[str, ...] = ("BTC_USDT", "ETH_USDT"),
    bar_limit_5m: int = 500,
    bar_limit_15m: int = 300,
    venue: str = GATE_TESTNET_VENUE,
    state_path: Path | None = None,
) -> BollingerSignalCycleSummary:
    """按周期优先级运行BTC/ETH共享资金、全局单持仓影子账户。"""
    health_by_stream = {
        (symbol, interval): refresh_market_health(symbol, interval, venue=venue)
        for symbol in symbols
        for interval in ("1m", "5m", "15m", "30m", "1h")
    }
    unhealthy = [
        f"{symbol} {interval}：{health.reason}"
        for (symbol, interval), health in health_by_stream.items()
        if health.status != "HEALTHY"
    ]
    if unhealthy:
        return BollingerSignalCycleSummary(
            status="BLOCKED_MARKET_HEALTH",
            new_signal_count=0,
            order_count=0,
            reason="；".join(unhealthy),
        )
    bars_by_symbol = {
        symbol: {
            # 5分钟是最高优先级震荡周期；确认走平后由1分钟行情负责触轨即时开仓。
            "5m": load_market_bars(
                symbol,
                "5m",
                limit=max(500, bar_limit_5m),
                venue=venue,
            ),
            "15m": load_market_bars(
                symbol,
                "15m",
                limit=max(300, bar_limit_15m),
                venue=venue,
            ),
            "30m": load_market_bars(symbol, "30m", limit=300, venue=venue),
            "1h": load_market_bars(symbol, "1h", limit=300, venue=venue),
        }
        for symbol in symbols
    }
    # 1分钟只供开仓过滤；状态机内部按UTC自然边界聚合3分钟，不加入交易周期优先级。
    micro_bars_by_symbol = {
        symbol: load_market_bars(
            symbol,
            "1m",
            limit=max(500, bar_limit_5m),
            venue=venue,
        )
        for symbol in symbols
    }
    # 调用多品种状态机；BTC/ETH与四个交易周期共享账户，1m/3m只负责过滤开仓。
    paper_kwargs = {"state_path": state_path} if state_path is not None else {}
    paper = run_multi_timeframe_paper_cycle(
        bars_by_symbol,
        micro_bars_by_symbol=micro_bars_by_symbol,
        **paper_kwargs,
    )
    entry_count = sum("模拟开仓" in item.title for item in paper.events)
    active_status = (
        f"{paper.active_symbol} {paper.active_interval}"
        if paper.active_symbol
        else (
            f"{paper.selected_symbol} {paper.selected_interval}"
            if paper.selected_symbol
            else "等待箱体"
        )
    )
    return BollingerSignalCycleSummary(
        status="SHADOW_RUNNING" if paper.status == "RUNNING" else paper.status,
        new_signal_count=entry_count,
        order_count=0,
        reason=f"{paper.reason}；当前选择：{active_status}",
        paper_status=paper.status,
        paper_equity=paper.equity,
        paper_events=paper.events,
        paper_holdings=paper.holdings,
    )
