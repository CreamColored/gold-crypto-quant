"""15分钟布林带轨道轮转的本地影子模拟周期。"""

from dataclasses import dataclass

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.market_health import refresh_market_health
from gold_crypto_quant.runtime.bollinger_rotation_simulator import (
    RotationPaperEvent,
    run_rotation_paper_cycle,
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
    """运行ETH 15分钟轨道轮转影子账户；永远不创建Gate订单。"""
    # 当前活动策略只使用15分钟行情，5分钟不再参与箱体判断或成交。
    health_15m = refresh_market_health(symbol, "15m", venue=GATE_TESTNET_VENUE)
    if health_15m.status != "HEALTHY":
        return BollingerSignalCycleSummary(
            status="BLOCKED_MARKET_HEALTH",
            new_signal_count=0,
            order_count=0,
            reason=f"15m：{health_15m.reason}",
        )
    bars = load_market_bars(
        symbol, "15m", limit=max(300, bar_limit_15m), venue=GATE_TESTNET_VENUE
    )
    # 调用本地状态机处理停机以来的新K线；该模块没有任何交易所下单方法。
    paper = run_rotation_paper_cycle(bars)
    entry_count = sum("模拟开仓" in item.title for item in paper.events)
    return BollingerSignalCycleSummary(
        status="SHADOW_RUNNING" if paper.status == "RUNNING" else paper.status,
        new_signal_count=entry_count,
        order_count=0,
        reason=paper.reason,
        paper_status=paper.status,
        paper_equity=paper.equity,
        paper_events=paper.events,
    )
