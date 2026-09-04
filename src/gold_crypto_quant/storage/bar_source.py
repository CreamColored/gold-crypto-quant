"""策略取K线的统一入口：优先 Redis，陈旧则降级 MySQL。

**降级判据是陈旧度，不是读取失败。** 采集服务挂掉之后 Redis 里的键还在、值还是旧的，
读取端不会收到任何错误——只比对心跳时间戳才能发现数据停了。用"读失败才切换"会让
策略拿着十分钟前的价格继续交易，而且一次告警都不发。

降级目标是 MySQL 而不是交易所 REST。MySQL 在本机、数据一直是全的（采集服务同时往
两边写）、读 500 根 90 毫秒，而且这条路径平时也在跑（回测走的就是它），不会出现
"真出事那天第一次执行"。降级去打交易所则要重新引入代理连通性、限流和 5 秒往返，
等于把刚搬走的网络 IO 又搬回策略进程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.redis_bars import read_bars, read_cursor

# 心跳超过这个秒数就认为采集服务的 Redis 视图不可信。采集服务每秒写一次心跳，
# 90 秒足以容忍一次重启（实测灌载约 0.5 秒）而不会误判。
STALE_AFTER_SECONDS = 90.0
HEARTBEAT_FIELD = "_heartbeat"


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """本轮取数用了哪一边，以及为什么。"""

    source: str
    reason: str
    heartbeat_age: float | None = None
    # 各(品种,周期)的最新收线时间。收线K线一分钟才变一次，而策略每秒都要跑；
    # 拿它当缓存键就能省掉每秒重读5700根的开销。
    cursor: dict[str, str] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        return self.source != "redis"


# 已加载的收线K线，键为 (交易所, 品种, 周期, 条数, 该序列的收线水位)。
# 水位没变就说明这段K线一根都没动，直接复用；变了自然落到新键上。
_BARS_CACHE: dict[tuple, pd.DataFrame] = {}
_BARS_CACHE_LIMIT = 128


def reset_bars_cache() -> None:
    """清空K线缓存；测试与切换数据源时调用。"""
    _BARS_CACHE.clear()


def check_health(
    venue: str, *, now: datetime | None = None, stale_after: float = STALE_AFTER_SECONDS
) -> SourceHealth:
    """判断该交易所的 Redis 视图是否新鲜。"""
    now = now or datetime.now(UTC)
    try:
        cursor = read_cursor(venue)
    except Exception as error:  # noqa: BLE001 - Redis 不可达直接降级，不抛给策略
        return SourceHealth("mysql", f"Redis不可达：{type(error).__name__}: {error}")
    raw = cursor.get(HEARTBEAT_FIELD)
    if not raw:
        return SourceHealth("mysql", "Redis中没有采集心跳")
    try:
        age = (now - datetime.fromisoformat(raw)).total_seconds()
    except ValueError:
        return SourceHealth("mysql", f"心跳时间戳无法解析：{raw!r}")
    if age > stale_after:
        return SourceHealth("mysql", f"采集心跳已停{age:.0f}秒", age)
    return SourceHealth(
        "redis", "", age, {k: v for k, v in cursor.items() if k != HEARTBEAT_FIELD}
    )


def load_bars(
    symbol: str,
    interval: str,
    *,
    venue: str,
    limit: int,
    health: SourceHealth,
    include_provisional: bool = False,
) -> pd.DataFrame:
    """按健康状态取K线；Redis 空或行数不足时同样退回 MySQL。

    行数不足也要退回：采集服务刚起、某个序列还没灌完时，Redis 里可能只有几十根，
    而布林带和MACD需要足够的预热长度，短序列算出来的轨道是错的。
    """
    if health.source == "redis":
        token = health.cursor.get(f"{symbol}:{interval}")
        key = (venue, symbol, interval, limit, include_provisional, token)
        if token:
            cached = _BARS_CACHE.get(key)
            if cached is not None:
                return cached
        try:
            frame = read_bars(
                venue, symbol, interval, limit=limit, include_provisional=include_provisional
            )
            if len(frame) >= min(limit, 200):
                frame = frame.drop(columns=["provisional"], errors="ignore")
                if token:
                    if len(_BARS_CACHE) >= _BARS_CACHE_LIMIT:
                        _BARS_CACHE.clear()
                    _BARS_CACHE[key] = frame
                return frame
        except Exception:  # noqa: BLE001 - 单条读失败就走 MySQL，不影响其它序列
            pass
    return load_market_bars(symbol, interval, venue=venue, limit=limit)


def load_provisional(
    symbol: str, *, venue: str, health: SourceHealth
) -> pd.Series | None:
    """取当前这一分钟的在途K线；没有或已降级时返回 None。

    降级期间不返回：在途K线只存在于 Redis，采集服务的视图既然已经陈旧，这根也
    不可信。降级后策略退回"只看收线K线"的老行为，这是安全的方向。
    """
    if health.degraded:
        return None
    try:
        frame = read_bars(venue, symbol, "1m", limit=2, include_provisional=True)
    except Exception:  # noqa: BLE001 - 读不到就当没有，策略退回收线K线
        return None
    if frame.empty or not bool(frame["provisional"].iloc[-1]):
        return None
    return frame.iloc[-1]
