"""用盘口合成"这一分钟还没结束"的那根K线，以及重启时把它补齐。

**只做1分钟一档。** 给5m/15m/30m/1h 也合成在途K线会有两个问题：盘口没有成交量，
而箱体判定里的量比正是在这些周期上算的，volume=0 会让判定失真；而策略对高周期只
读已确认的收线K线（``confirmed_context_at``），本来就不消费在途值。

用中间价而不是买一：与交易所官方收线价对比实测，中间价的偏差为 BTC 0.03-0.06bp、
ETH 0.06-0.09bp、XAU 0.05-0.19bp，比手续费(2-5bp)小两个数量级。

时间归属用交易所推送里的时间戳，不用本机时钟。本机和交易所有偏差，跨代理更明显，
用本机时钟会让K线边界错位。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, MarketQuoteSecond
from gold_crypto_quant.storage.redis_bars import RedisBar

MINUTE = timedelta(minutes=1)


def floor_to_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


@dataclass(slots=True)
class ProvisionalMinute:
    """当前这一分钟的在途K线累加器。"""

    open_time: datetime
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def start(cls, open_time: datetime, price: float) -> ProvisionalMinute:
        return cls(open_time, price, price, price, price)

    def fold(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price

    def to_bar(self) -> RedisBar:
        # 成交量留0：盘口推送里没有成交量，而1分钟序列的量不参与任何判定，
        # 编造一个数字反而会让人以为它有意义。
        return RedisBar(
            open_time=self.open_time,
            open=self.open, high=self.high, low=self.low, close=self.close,
            volume=0.0, quote_volume=0.0,
            provisional=True,
        )


class ProvisionalTracker:
    """按(交易所,品种)维护在途K线；跨分钟自动翻页。"""

    def __init__(self) -> None:
        self._bars: dict[tuple[str, str], ProvisionalMinute] = {}

    def observe(self, venue: str, symbol: str, price: float, moment: datetime) -> None:
        """并入一帧；``moment`` 必须是交易所时间戳。"""
        minute = floor_to_minute(moment)
        key = (venue, symbol)
        current = self._bars.get(key)
        if current is None or current.open_time != minute:
            self._bars[key] = ProvisionalMinute.start(minute, price)
        else:
            current.fold(price)

    def snapshot(self) -> dict[tuple[str, str], RedisBar]:
        """当前所有在途K线；调用方负责写入 Redis。"""
        return {key: bar.to_bar() for key, bar in self._bars.items()}

    def seed(self, venue: str, symbol: str, bar: ProvisionalMinute) -> None:
        self._bars[(venue, symbol)] = bar


def rebuild_from_seconds(
    venue: str,
    symbol: str,
    *,
    now: datetime | None = None,
    engine: Engine | None = None,
) -> ProvisionalMinute | None:
    """重启时用秒级历史把当前这一分钟已经过去的部分补回来。

    采集服务在 10:35:40 重启时，10:35 这根在途K线的前40秒它没收到——open 是错的，
    最高最低也是错的。秒级表里有那40秒，直接补。

    补不出来（表里这一分钟没有数据，比如停了很久）时返回 None，由调用方从下一帧
    重新开始累加。
    """
    now = now or datetime.now(UTC)
    minute = floor_to_minute(now)
    engine = engine or build_engine()
    statement = (
        select(
            MarketQuoteSecond.bucket_time,
            MarketQuoteSecond.bid_low, MarketQuoteSecond.bid_high,
            MarketQuoteSecond.ask_low, MarketQuoteSecond.ask_high,
            MarketQuoteSecond.bid_close, MarketQuoteSecond.ask_close,
        )
        .join(Instrument, Instrument.id == MarketQuoteSecond.instrument_id)
        .where(
            Instrument.venue == venue,
            Instrument.symbol == symbol,
            MarketQuoteSecond.bucket_time >= minute,
            MarketQuoteSecond.bucket_time < minute + MINUTE,
        )
        .order_by(MarketQuoteSecond.bucket_time)
    )
    with Session(engine) as session:
        rows = session.execute(statement).all()
    if not rows:
        return None

    def mid(low, high) -> float:
        return (float(low) + float(high)) / 2

    first = rows[0]
    # 秒桶存的是极值，中间价的上下界要用同侧极值配对：最高中间价来自买一高与卖一高。
    opening = mid(first.bid_close or first.bid_low, first.ask_close or first.ask_high)
    bar = ProvisionalMinute.start(minute, opening)
    for row in rows:
        bar.fold(mid(row.bid_high, row.ask_high))
        bar.fold(mid(row.bid_low, row.ask_low))
    last = rows[-1]
    bar.close = mid(last.bid_close or last.bid_low, last.ask_close or last.ask_high)
    return bar
