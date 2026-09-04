"""Redis 里的K线视图：500根官方收线K线 + 第501根在途K线。

结构上有界：每次写完裁到501根，跑一天和跑一年占用一样。这是"策略进程崩了采集服务
要不要继续写"的答案——不靠消费者活着来控制内存。

前500根是交易所官方K线，和 MySQL、和回测同源；第501根是用盘口合成的、这一分钟还
没结束的那根，本来就不存在官方版本。官方收线K线一到就把它顶掉，因此合成数据只活在
"当前周期尚未结束"这个窗口里，历史永远是官方的。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
import redis

from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.redis_client import bars_key, build_redis, cursor_key

# 500根收线 + 1根在途。策略读取时按 provisional 标记区分。
CLOSED_BAR_LIMIT = 500
TOTAL_BAR_LIMIT = CLOSED_BAR_LIMIT + 1

# 同一个 score 上塞两个不同 JSON 会并存而不是覆盖——ZSET 成员是字符串，score 只是
# 排序键。因此必须先按 score 精确删除再写，并且这三步要对读取方原子可见。
_UPSERT_LUA = """
for i = 1, #ARGV, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[1], ARGV[i], ARGV[i])
  redis.call('ZADD', KEYS[1], ARGV[i], ARGV[i + 1])
end
redis.call('ZREMRANGEBYRANK', KEYS[1], 0, -1 - tonumber(KEYS[2]))
return redis.call('ZCARD', KEYS[1])
"""


@dataclass(frozen=True, slots=True)
class RedisBar:
    """一根K线在 Redis 中的形态。"""

    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    # 在途K线：这一周期还没结束，用盘口合成。官方收线K线到达后会把它按 score 顶掉。
    provisional: bool = False

    def to_member(self) -> str:
        payload = {
            "t": self.open_time.isoformat(),
            "o": self.open, "h": self.high, "l": self.low, "c": self.close,
            "v": self.volume, "q": self.quote_volume,
        }
        if self.provisional:
            payload["p"] = 1
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @property
    def score(self) -> int:
        return int(self.open_time.timestamp())


def _from_member(raw: str) -> RedisBar:
    d = json.loads(raw)
    return RedisBar(
        open_time=datetime.fromisoformat(d["t"]),
        open=d["o"], high=d["h"], low=d["l"], close=d["c"],
        volume=d["v"], quote_volume=d["q"],
        provisional=bool(d.get("p")),
    )


def _script(client: redis.Redis):
    # register_script 只在客户端本地登记，首次 evalsha 未命中会自动回退到 eval。
    return client.register_script(_UPSERT_LUA)


def publish_bars(
    venue: str,
    symbol: str,
    interval: str,
    bars: list[RedisBar],
    *,
    client: redis.Redis | None = None,
    limit: int = TOTAL_BAR_LIMIT,
) -> int:
    """写入或替换若干根K线，并把该序列裁到 ``limit`` 根。返回写入后的总根数。"""
    if not bars:
        return 0
    client = client or build_redis()
    args: list = []
    for bar in bars:
        args.extend((bar.score, bar.to_member()))
    return int(_script(client)(keys=[bars_key(venue, symbol, interval), limit], args=args))


def read_bars(
    venue: str,
    symbol: str,
    interval: str,
    *,
    client: redis.Redis | None = None,
    limit: int | None = None,
    include_provisional: bool = True,
) -> pd.DataFrame:
    """读回K线，返回与 ``load_market_bars`` 同构的 DataFrame（UTC升序索引）。

    列名和 dtype 都对齐 MySQL 那条路径，策略层因此不需要知道数据来自哪边——降级读
    MySQL 时不用走另一套解析。
    """
    client = client or build_redis()
    raw = client.zrange(bars_key(venue, symbol, interval), 0, -1)
    parsed = [_from_member(item) for item in raw]
    if not include_provisional:
        parsed = [bar for bar in parsed if not bar.provisional]
    if limit is not None:
        parsed = parsed[-limit:]
    frame = pd.DataFrame(
        [
            {
                "open_time": bar.open_time, "open": bar.open, "high": bar.high,
                "low": bar.low, "close": bar.close, "volume": bar.volume,
                "quote_volume": bar.quote_volume, "provisional": bar.provisional,
            }
            for bar in parsed
        ],
        columns=["open_time", "open", "high", "low", "close", "volume",
                 "quote_volume", "provisional"],
    )
    if frame.empty:
        return frame.set_index("open_time")
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    return frame.set_index("open_time").sort_index()


def publish_cursor(
    venue: str,
    entries: dict[tuple[str, str], datetime],
    *,
    client: redis.Redis | None = None,
) -> None:
    """更新该交易所各(品种,周期)的最新收线时间。

    策略每秒只读这一个键判断有没有新收线，命中率约1/60；没变就不去读整份K线，
    59/60的轮次因此只花约0.1毫秒。
    """
    if not entries:
        return
    client = client or build_redis()
    client.hset(
        cursor_key(venue),
        mapping={f"{symbol}:{interval}": moment.isoformat()
                 for (symbol, interval), moment in entries.items()},
    )


def read_cursor(venue: str, *, client: redis.Redis | None = None) -> dict[str, str]:
    """读回该交易所的收线水位；键形如 ``BTC_USDT:1m``。"""
    client = client or build_redis()
    return dict(client.hgetall(cursor_key(venue)) or {})


def publish_heartbeat(
    venue: str, moment: datetime | None = None, *, client: redis.Redis | None = None
) -> None:
    """写入采集服务的心跳时刻，供读取端判断陈旧度。

    采集服务挂掉后 Redis 里的键还在、值还是旧的，读取端不会收到任何错误——陈旧度
    只能靠时间戳发现，不能靠读取失败。
    """
    client = client or build_redis()
    client.hset(cursor_key(venue), "_heartbeat", (moment or datetime.now(UTC)).isoformat())


def bootstrap_from_mysql(
    venue: str,
    symbols: tuple[str, ...],
    intervals: tuple[str, ...],
    *,
    client: redis.Redis | None = None,
    limit: int = CLOSED_BAR_LIMIT,
    reporter: Callable[[str], None] | None = None,
) -> dict[tuple[str, str], datetime]:
    """重启时用 MySQL 灌满 Redis，并返回各(品种,周期)的最新收线时间。

    不走 REST：同样的数据 MySQL 里都有，本机读500根约90毫秒，而30个REST请求即使
    并发也要2秒，还白占交易所限流额度。

    单个(品种,周期)读失败不中止整体——Redis 里缺哪条，读取端按陈旧度降级读 MySQL
    就是了，没必要因为一条缺失让采集服务起不来。
    """
    client = client or build_redis()
    cursor: dict[tuple[str, str], datetime] = {}
    for symbol in symbols:
        for interval in intervals:
            try:
                frame = load_market_bars(symbol, interval, venue=venue, limit=limit)
            except Exception as error:  # noqa: BLE001 - 缺一条不该让采集服务起不来
                if reporter is not None:
                    reporter(
                        f"灌载{venue} {symbol} {interval}失败："
                        f"{type(error).__name__}: {error}"
                    )
                continue
            bars = [
                RedisBar(
                    open_time=index.to_pydatetime(),
                    open=float(row.open), high=float(row.high), low=float(row.low),
                    close=float(row.close), volume=float(row.volume),
                    quote_volume=float(row.quote_volume),
                )
                for index, row in frame.iterrows()
            ]
            if not bars:
                continue
            publish_bars(venue, symbol, interval, bars, client=client)
            cursor[(symbol, interval)] = bars[-1].open_time
    publish_cursor(venue, cursor, client=client)
    return cursor
