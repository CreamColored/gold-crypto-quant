"""15分钟布林带轨道轮转的本地影子模拟周期。"""

from dataclasses import dataclass
from pathlib import Path

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.runtime.bollinger_rotation_simulator import RotationPaperEvent
from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    run_multi_timeframe_paper_cycle,
)
from gold_crypto_quant.storage.bar_source import (
    SourceHealth,
    check_health,
    load_bars,
    load_provisional,
)
from gold_crypto_quant.storage.market_health import refresh_market_health
from gold_crypto_quant.storage.trading_switches import load_switches, resolve_entry_allowed


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
    # 在途K线的观察情况，供每分钟心跳日志展示。
    paper_provisional_watch: str = ""


# 各交易所上一轮的行情健康判定；没有新收线K线时复用。
_HEALTH_CACHE: dict[str, dict] = {}


def reset_health_cache() -> None:
    """清空健康判定缓存；测试与切换数据源时调用。"""
    _HEALTH_CACHE.clear()


def run_bollinger_signal_cycle(
    *,
    symbols: tuple[str, ...] = ("BTC_USDT", "ETH_USDT"),
    bar_limit_5m: int = 500,
    bar_limit_15m: int = 300,
    venue: str = GATE_TESTNET_VENUE,
    state_path: Path | None = None,
    source_health: SourceHealth | None = None,
    refresh_health: bool = True,
) -> BollingerSignalCycleSummary:
    """按周期优先级运行BTC/ETH共享资金、全局单持仓影子账户。

    ``source_health`` 决定这一轮从 Redis 还是 MySQL 取K线；不传则自行判定，方便
    脚本和测试直接调用。
    """
    source_health = source_health or check_health(venue)
    # 健康判定的输入是"最后一根收线K线的时间"，一分钟才变一次；而策略每秒都跑。
    # 每轮重算要发75条SQL（15个流 × 查品种、查末根、查旧状态、写新状态、提交），
    # 两个交易所每秒就是150条——实测占了数据库全部负载的九成以上。
    # 没有新收线时直接复用上一轮的判定。
    if refresh_health or venue not in _HEALTH_CACHE:
        _HEALTH_CACHE[venue] = {
            (symbol, interval): refresh_market_health(symbol, interval, venue=venue)
            for symbol in symbols
            for interval in ("1m", "5m", "15m", "30m", "1h")
        }
    health_by_stream = _HEALTH_CACHE[venue]
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
            "5m": load_bars(
                symbol, "5m", venue=venue, limit=max(500, bar_limit_5m), health=source_health
            ),
            "15m": load_bars(
                symbol, "15m", venue=venue, limit=max(300, bar_limit_15m), health=source_health
            ),
            "30m": load_bars(symbol, "30m", venue=venue, limit=300, health=source_health),
            "1h": load_bars(symbol, "1h", venue=venue, limit=300, health=source_health),
        }
        for symbol in symbols
    }
    # 1分钟只供开仓过滤；状态机内部按UTC自然边界聚合3分钟，不加入交易周期优先级。
    micro_bars_by_symbol = {
        symbol: load_bars(
            symbol, "1m", venue=venue, limit=max(500, bar_limit_5m), health=source_health
        )
        for symbol in symbols
    }
    # 调用多品种状态机；BTC/ETH与四个交易周期共享账户，1m/3m只负责过滤开仓。
    paper_kwargs = {"state_path": state_path} if state_path is not None else {}
    # 交易开关只挡开仓：关闭后已有仓位的止损、减仓与止盈照常执行。
    switches = load_switches()
    # 在途K线只喂触轨判定，不参与指标计算——把未收线的那根算进布林带和MACD，
    # 轨道会在分钟内不停抖动，且与回测口径分叉。
    provisional_by_symbol = {
        symbol: bar
        for symbol in symbols
        if (bar := load_provisional(symbol, venue=venue, health=source_health)) is not None
    }
    paper = run_multi_timeframe_paper_cycle(
        bars_by_symbol,
        micro_bars_by_symbol=micro_bars_by_symbol,
        provisional_by_symbol=provisional_by_symbol,
        entry_allowed=lambda symbol: resolve_entry_allowed(switches, venue, symbol),
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
        paper_provisional_watch=paper.provisional_watch,
    )
