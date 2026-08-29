"""行情心跳和已收盘K线新鲜度状态机。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from gold_crypto_quant.market_data.gate_history import INTERVAL_DURATION


class MarketHealthStatus(StrEnum):
    """订单入口可以识别的行情健康状态。"""

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class MarketHealthEvaluation:
    """一个品种周期在指定检查时刻的健康结果。"""

    status: MarketHealthStatus
    stale_seconds: int | None
    reason: str


def evaluate_market_health(
    *,
    now: datetime,
    interval: str,
    last_bar_close_time: datetime | None,
    last_heartbeat_at: datetime | None = None,
    maximum_stale_intervals: int = 2,
    heartbeat_timeout_seconds: int = 120,
) -> MarketHealthEvaluation:
    """依次检查心跳超时、行情缺失、未来时间和K线过期。"""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if interval not in INTERVAL_DURATION:
        raise ValueError(f"unsupported market health interval: {interval}")
    if maximum_stale_intervals < 1 or heartbeat_timeout_seconds < 1:
        raise ValueError("health thresholds must be positive")
    if last_heartbeat_at is not None:
        if last_heartbeat_at.tzinfo is None:
            raise ValueError("last_heartbeat_at must be timezone-aware")
        heartbeat_age = (now - last_heartbeat_at).total_seconds()
        if heartbeat_age < -1:
            return MarketHealthEvaluation(
                status=MarketHealthStatus.ERROR,
                stale_seconds=None,
                reason="行情心跳时间晚于当前系统时间",
            )
        if heartbeat_age > heartbeat_timeout_seconds:
            return MarketHealthEvaluation(
                status=MarketHealthStatus.TIMEOUT,
                stale_seconds=None,
                reason=f"行情心跳已超时{int(heartbeat_age)}秒",
            )
    if last_bar_close_time is None:
        return MarketHealthEvaluation(
            status=MarketHealthStatus.ERROR,
            stale_seconds=None,
            reason="数据库中不存在已收盘K线",
        )
    if last_bar_close_time.tzinfo is None:
        raise ValueError("last_bar_close_time must be timezone-aware")

    stale_seconds = int((now - last_bar_close_time).total_seconds())
    if stale_seconds < -1:
        return MarketHealthEvaluation(
            status=MarketHealthStatus.ERROR,
            stale_seconds=stale_seconds,
            reason="最新K线收盘时间晚于当前系统时间",
        )
    maximum_stale_seconds = int(
        INTERVAL_DURATION[interval].total_seconds() * maximum_stale_intervals
    )
    if stale_seconds > maximum_stale_seconds:
        return MarketHealthEvaluation(
            status=MarketHealthStatus.STALE,
            stale_seconds=stale_seconds,
            reason=f"最新K线已过期{stale_seconds}秒，超过{maximum_stale_seconds}秒限制",
        )
    return MarketHealthEvaluation(
        status=MarketHealthStatus.HEALTHY,
        stale_seconds=max(stale_seconds, 0),
        reason="行情心跳与已收盘K线时间正常",
    )
