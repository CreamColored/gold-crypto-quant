"""15分钟布林带轨道轮转的本地影子模拟周期。"""

from dataclasses import dataclass

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.market_health import refresh_market_health
from gold_crypto_quant.runtime.bollinger_rotation_simulator import RotationPaperEvent
from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    run_multi_timeframe_paper_cycle,
)


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


def run_bollinger_signal_cycle(
    *,
    symbol: str = "ETH_USDT",
    bar_limit_5m: int = 500,
    bar_limit_15m: int = 300,
) -> BollingerSignalCycleSummary:
    """按5m、15m、30m、1h优先级运行ETH单持仓影子账户。"""
    health_by_interval = {
        interval: refresh_market_health(symbol, interval, venue=GATE_TESTNET_VENUE)
        for interval in ("5m", "15m", "30m", "1h")
    }
    unhealthy = [
        f"{interval}：{health.reason}"
        for interval, health in health_by_interval.items()
        if health.status != "HEALTHY"
    ]
    if unhealthy:
        return BollingerSignalCycleSummary(
            status="BLOCKED_MARKET_HEALTH",
            new_signal_count=0,
            order_count=0,
            reason="；".join(unhealthy),
        )
    bars_by_interval = {
        "5m": load_market_bars(
            symbol, "5m", limit=max(500, bar_limit_5m), venue=GATE_TESTNET_VENUE
        ),
        "15m": load_market_bars(
            symbol, "15m", limit=max(300, bar_limit_15m), venue=GATE_TESTNET_VENUE
        ),
        "30m": load_market_bars(
            symbol, "30m", limit=300, venue=GATE_TESTNET_VENUE
        ),
        "1h": load_market_bars(
            symbol, "1h", limit=300, venue=GATE_TESTNET_VENUE
        ),
    }
    # 调用多周期本地状态机；四个周期共享一个账户，始终最多只有一个方向仓位。
    paper = run_multi_timeframe_paper_cycle(bars_by_interval)
    entry_count = sum("模拟开仓" in item.title for item in paper.events)
    interval_status = paper.active_interval or paper.selected_interval or "等待箱体"
    return BollingerSignalCycleSummary(
        status="SHADOW_RUNNING" if paper.status == "RUNNING" else paper.status,
        new_signal_count=entry_count,
        order_count=0,
        reason=f"{paper.reason}；当前周期：{interval_status}",
        paper_status=paper.status,
        paper_equity=paper.equity,
        paper_events=paper.events,
    )
