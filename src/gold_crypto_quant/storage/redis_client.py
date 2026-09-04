"""Redis 连接与键空间约定。

采集服务写、策略进程读。Redis 在这套系统里**只承载可重建的热数据**——现价快照和
最近若干根K线；真身始终在 MySQL。因此这里全部使用有界结构，并且任何一个读取点都
必须能在 Redis 不可用时退回 MySQL：Redis 挂掉应该让系统变慢，不应该让它停摆。

键空间用 ``redis_key_prefix``（默认 ``gcq``）隔开，与同一实例上其它应用互不干扰。
"""

from __future__ import annotations

from functools import lru_cache

import redis

from gold_crypto_quant.config import get_settings

# 采集服务每秒都在写，连不上时不能让它卡住整个事件循环。
CONNECT_TIMEOUT_SECONDS = 2.0
OPERATION_TIMEOUT_SECONDS = 2.0


@lru_cache
def build_redis() -> redis.Redis:
    """返回进程内唯一的 Redis 客户端。

    和 ``build_engine`` 同样做成单例：redis-py 的连接池挂在客户端对象上，每次新建
    等于每次重建连接池，之前 SQLAlchemy Engine 没做单例时单轮多花了26秒。
    """
    settings = get_settings()
    password = settings.redis_password
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=password.get_secret_value() if password is not None else None,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=OPERATION_TIMEOUT_SECONDS,
        decode_responses=True,
        health_check_interval=30,
    )


def reset_redis() -> None:
    """丢弃缓存的客户端；仅供测试与配置热更新使用。"""
    build_redis.cache_clear()


@lru_cache
def _prefix() -> str:
    return get_settings().redis_key_prefix


def quote_key(venue: str, symbol: str) -> str:
    """某个交易所某个品种的最新买一卖一快照（Hash）。

    固定键数（交易所×品种），写多久都不增长——这是"双行情崩了采集服务要不要继续写"
    这个问题的答案：结构本身自限长，就不需要靠消费者活着来控制内存。
    """
    return f"{_prefix()}:quote:{venue}:{symbol}"


def bars_key(venue: str, symbol: str, interval: str) -> str:
    """最近若干根K线（ZSET，score 为 open_time 的秒级时间戳）。"""
    return f"{_prefix()}:bars:{venue}:{symbol}:{interval}"


def cursor_key(venue: str) -> str:
    """该交易所各周期最新收线时间（Hash）。

    策略每秒只读这一个键判断"有没有新收线"，命中率约 1/60；只有变了才去读整份
    K线。59/60 的轮次因此只花约 0.1 毫秒。
    """
    return f"{_prefix()}:cursor:{venue}"
