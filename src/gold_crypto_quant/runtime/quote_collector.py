"""通过 WebSocket 采集 Gate 与币安永续盘口，按秒和按分钟聚合入库。

为什么只存聚合而不存每一帧：盘口每秒推上千帧（实测币安BTC约1424帧/秒），
三品种两交易所合计约2868帧/秒，原样入库是每天2.48亿行、13.85GB。而策略判定的是
"这段时间里有没有碰到轨道"，只要保住区间内的最低买一与最高卖一，信息就一点不少，
数据量降到每天51.8万行（秒级）加8640行（分钟级）。

为什么用 bookTicker 而不是 kline：币安期货的 aggTrade / kline / markPrice / ticker
在本项目网络下一帧不推，只有 bookTicker 与 depth5 正常；Gate 各通道都正常。
两家唯一都可用的是 bookTicker，双所对照要求口径一致，因此只能取这一个。

本模块只采集，不参与任何交易判定；策略仍按已收盘K线运行。
"""

import asyncio
import json
import signal
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import websockets
from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.exchanges.binance import BinancePublicClient
from gold_crypto_quant.exchanges.gate import GatePublicClient
from gold_crypto_quant.market_data.binance_history import BINANCE_LIVE_VENUE, INTERNAL_TO_BINANCE
from gold_crypto_quant.market_data.gate_history import GATE_LIVE_VENUE
from gold_crypto_quant.market_data.live_feed import (
    refresh_binance_live_bars,
    refresh_gate_live_bars,
)
from gold_crypto_quant.runtime.provisional_bars import (
    ProvisionalTracker,
    rebuild_from_seconds,
)
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, MarketQuoteMinute, MarketQuoteSecond
from gold_crypto_quant.storage.redis_bars import (
    RedisBar,
    bootstrap_from_mysql,
    publish_bars,
    publish_cursor,
    publish_heartbeat,
)
from gold_crypto_quant.storage.redis_client import build_redis, quote_key

QUOTE_CONTRACTS = ("BTC_USDT", "ETH_USDT", "XAU_USDT")
BINANCE_WS_URL = "wss://fstream.binance.com/stream?streams="
GATE_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
# 断线重连退避；上限不宜太大，盘口断开期间无法事后补齐。
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 20.0)
SECOND_FLUSH_INTERVAL = 1.0
# 每秒把在途K线推给 Redis；策略据此在分钟内也能看到价格变化。
PROVISIONAL_PUBLISH_INTERVAL = 1.0
# 收线后等这么久再拉。所有周期（1m/5m/15m/30m/1h）都对齐UTC整分边界，因此只要在
# 每个整分之后醒一次就够；固定节拍会平均多等半个节拍，而这段延迟直接加在
# "收线到策略做出判断"的链路上。留0.3秒是给交易所自己的收线写入留余量。
BAR_CLOSE_SETTLE_SECONDS = 0.3
PUBLIC_INTERVALS = ("1m", "5m", "15m", "30m", "1h")


@dataclass(slots=True)
class QuoteBucket:
    """一个时间桶内的盘口极值。

    只保极值：触轨判定问的是"有没有碰到轨道"，桶内的价格路径对该判定没有信息量。
    """

    bid_low: float
    bid_high: float
    ask_low: float
    ask_high: float
    frame_count: int
    # 桶内最后一帧的原始买卖对。极值来自秒内不同瞬间，拼在一起会得到
    # 买一高于卖一的交叉盘口——展示当前价必须用同一帧的快照。
    bid_close: float = 0.0
    ask_close: float = 0.0

    @classmethod
    def start(cls, bid: float, ask: float) -> "QuoteBucket":
        return cls(bid, bid, ask, ask, 1, bid, ask)

    def fold(self, bid: float, ask: float) -> None:
        """并入一帧盘口。"""
        self.bid_low = min(self.bid_low, bid)
        self.bid_high = max(self.bid_high, bid)
        self.ask_low = min(self.ask_low, ask)
        self.ask_high = max(self.ask_high, ask)
        self.bid_close, self.ask_close = bid, ask
        self.frame_count += 1

    def merge(self, other: "QuoteBucket") -> None:
        """并入另一个桶；秒级汇总成分钟级时使用，结果与直接聚合原始帧完全一致。"""
        self.bid_low = min(self.bid_low, other.bid_low)
        self.bid_high = max(self.bid_high, other.bid_high)
        self.ask_low = min(self.ask_low, other.ask_low)
        self.ask_high = max(self.ask_high, other.ask_high)
        # 后来的桶在时间上更靠后，快照取它的。
        self.bid_close, self.ask_close = other.bid_close, other.ask_close
        self.frame_count += other.frame_count


def floor_to_second(moment: datetime) -> datetime:
    return moment.replace(microsecond=0)


def floor_to_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


class QuoteCollector:
    """维护两条 WebSocket 长连接，把盘口聚合成秒级与分钟级两张表。"""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        contracts: tuple[str, ...] = QUOTE_CONTRACTS,
        reporter: Callable[[str], None] | None = None,
    ) -> None:
        self.engine = engine or build_engine()
        self.contracts = contracts
        self.reporter = reporter or (lambda _message: None)
        self._instrument_ids: dict[tuple[str, str], int] = {}
        # 键为 (venue, 内部品种代码, 桶起始时间)；未落盘的桶都在内存里。
        self._seconds: dict[tuple[str, str, datetime], QuoteBucket] = {}
        self._minutes: dict[tuple[str, str, datetime], QuoteBucket] = {}
        # 分钟桶实际覆盖了多少秒；小于60即说明该分钟内断过线。
        self._minute_seconds: dict[tuple[str, str, datetime], int] = {}
        self._stopping = asyncio.Event()
        # 在途K线：本分钟还没结束的那根，用盘口逐帧累加。
        self._provisional = ProvisionalTracker()
        # 各交易所的K线刷新水位；只有到期的周期才发REST请求。
        self._refreshed: dict[str, dict[str, datetime]] = {
            GATE_LIVE_VENUE: {}, BINANCE_LIVE_VENUE: {},
        }

    def load_instruments(self) -> None:
        """解析 (交易所, 品种) 到 instrument_id；缺失的品种直接报错，不静默跳过。"""
        with Session(self.engine) as session:
            for venue in (GATE_LIVE_VENUE, BINANCE_LIVE_VENUE):
                for contract in self.contracts:
                    found = session.execute(
                        select(Instrument.id).where(
                            Instrument.venue == venue, Instrument.symbol == contract
                        )
                    ).scalar_one_or_none()
                    if found is None:
                        raise RuntimeError(f"instrument not found: {venue} {contract}")
                    self._instrument_ids[(venue, contract)] = found

    def record(self, venue: str, contract: str, bid: float, ask: float) -> None:
        """并入一帧盘口；本方法在事件循环里被高频调用，必须保持廉价。"""
        key = (venue, contract, floor_to_second(datetime.now(UTC)))
        bucket = self._seconds.get(key)
        if bucket is None:
            self._seconds[key] = QuoteBucket.start(bid, ask)
        else:
            bucket.fold(bid, ask)

    def drain_seconds(self, now: datetime) -> list[tuple]:
        """取出所有已经走完的秒桶，并顺带汇总进对应的分钟桶。"""
        current = floor_to_second(now)
        ready = [key for key in self._seconds if key[2] < current]
        rows = []
        for key in ready:
            bucket = self._seconds.pop(key)
            venue, contract, second = key
            rows.append((venue, contract, second, bucket))
            minute_key = (venue, contract, floor_to_minute(second))
            existing = self._minutes.get(minute_key)
            if existing is None:
                self._minutes[minute_key] = QuoteBucket(
                    bucket.bid_low, bucket.bid_high, bucket.ask_low,
                    bucket.ask_high, bucket.frame_count,
                    bucket.bid_close, bucket.ask_close,
                )
                self._minute_seconds[minute_key] = 1
            else:
                existing.merge(bucket)
                self._minute_seconds[minute_key] += 1
        return rows

    def drain_minutes(self, now: datetime) -> list[tuple]:
        """取出所有已经走完的分钟桶。"""
        current = floor_to_minute(now)
        ready = [key for key in self._minutes if key[2] < current]
        rows = []
        for key in ready:
            bucket = self._minutes.pop(key)
            covered = self._minute_seconds.pop(key, 0)
            venue, contract, minute = key
            rows.append((venue, contract, minute, bucket, covered))
        return rows

    def _track_provisional(
        self, venue: str, contract: str, bid: float, ask: float, exchange_ms: object
    ) -> None:
        """把一帧盘口并进在途K线。

        时间归属优先用交易所推送里的时间戳；字段缺失或不是数字时才退回本机时钟——
        退回是为了不丢帧，但边界可能错位，因此只作为兜底。
        """
        try:
            moment = datetime.fromtimestamp(float(exchange_ms) / 1000, tz=UTC)
        except (TypeError, ValueError):
            moment = datetime.now(UTC)
        self._provisional.observe(venue, contract, (bid + ask) / 2, moment)

    def _publish_provisional(self) -> int:
        """把各品种的在途K线写进 Redis 的第501根位置。

        只写1分钟一档：盘口没有成交量，而箱体判定的量比是在5m及以上算的，给高周期
        合成在途K线会让量比失真；策略对高周期本来也只读已确认的收线K线。
        """
        snapshot = self._provisional.snapshot()
        if not snapshot:
            return 0
        try:
            client = build_redis()
            for (venue, contract), bar in snapshot.items():
                publish_bars(venue, contract, "1m", [bar], client=client)
            for venue in (GATE_LIVE_VENUE, BINANCE_LIVE_VENUE):
                publish_heartbeat(venue, client=client)
        except Exception as error:  # noqa: BLE001 - Redis 故障不能拖垮采集
            self.reporter(f"在途K线写入失败：{type(error).__name__}: {error}")
            return 0
        return len(snapshot)

    def refresh_bars(self) -> int:
        """拉取到期周期的官方K线，写 MySQL 与 Redis。

        这一步原先在量化服务进程里。搬过来之后策略进程不再持有交易所客户端，采集与
        策略彻底分开：拉取失败只影响这个进程，策略靠 Redis 陈旧度降级读 MySQL 继续跑。
        """
        total = 0
        for venue, refresh, client_attr in (
            (GATE_LIVE_VENUE, refresh_gate_live_bars, "_gate_client"),
            (BINANCE_LIVE_VENUE, refresh_binance_live_bars, "_binance_client"),
        ):
            client = getattr(self, client_attr, None)
            if client is None:
                continue
            kwargs = {"venue": venue} if venue == GATE_LIVE_VENUE else {}
            results = refresh(
                client,
                contracts=self.contracts,
                intervals=PUBLIC_INTERVALS,
                refreshed=self._refreshed[venue],
                limit=500,
                **kwargs,
            )
            if not results:
                continue
            total += self._publish_closed_bars(venue, {r.interval for r in results})
        return total

    def _publish_closed_bars(self, venue: str, intervals: set[str]) -> int:
        """把刚落库的官方K线同步进 Redis，并推进 cursor。

        官方K线按 open_time 精确替换同一位置上的在途K线——ZSET 成员是字符串，同一
        score 塞两个不同 JSON 会并存而不是覆盖，因此必须先删再写（见 redis_bars）。
        """
        from gold_crypto_quant.storage.market_bars import load_market_bars

        published = 0
        cursor: dict[tuple[str, str], datetime] = {}
        try:
            client = build_redis()
            for contract in self.contracts:
                for interval in sorted(intervals):
                    frame = load_market_bars(contract, interval, venue=venue, limit=200)
                    bars = [
                        RedisBar(
                            open_time=index.to_pydatetime(),
                            open=float(row.open), high=float(row.high),
                            low=float(row.low), close=float(row.close),
                            volume=float(row.volume), quote_volume=float(row.quote_volume),
                        )
                        for index, row in frame.iterrows()
                    ]
                    if not bars:
                        continue
                    publish_bars(venue, contract, interval, bars, client=client)
                    cursor[(contract, interval)] = bars[-1].open_time
                    published += len(bars)
            publish_cursor(venue, cursor, client=client)
        except Exception as error:  # noqa: BLE001 - Redis 故障不能拖垮采集
            self.reporter(f"K线同步Redis失败：{type(error).__name__}: {error}")
        return published

    def _publish_snapshots(self, drained: list[tuple]) -> None:
        """把每个品种最新一秒的盘口写进 Redis，供策略与前端读当前价。

        只写每个品种的**最后一秒**：Redis 这份是快照不是流水，历史在 MySQL。键数固定
        为交易所×品种，写多久都不增长——不需要靠读取方活着来控制内存。

        Redis 不可用绝不能影响采集：这里吞掉异常只记一行日志，MySQL 那条链路照常。
        Redis 存的全部是可重建的热数据，丢了下一秒就补回来。
        """
        if not drained:
            return
        latest: dict[tuple[str, str], tuple[datetime, QuoteBucket]] = {}
        for venue, contract, bucket_time, bucket in drained:
            key = (venue, contract)
            if key not in latest or bucket_time > latest[key][0]:
                latest[key] = (bucket_time, bucket)
        try:
            client = build_redis()
            pipe = client.pipeline(transaction=False)
            for (venue, contract), (bucket_time, bucket) in latest.items():
                pipe.hset(
                    quote_key(venue, contract),
                    mapping={
                        "bid": bucket.bid_close,
                        "ask": bucket.ask_close,
                        # 陈旧度判定用的是这个时间戳。采集服务挂掉后 Redis 里的键还在、
                        # 值还是旧的，读取端不会收到任何错误——只能靠时间戳发现。
                        "ts": bucket_time.isoformat(),
                        "frames": bucket.frame_count,
                    },
                )
            pipe.execute()
        except Exception as error:  # noqa: BLE001 - Redis 故障不能拖垮采集
            self.reporter(f"Redis快照写入失败：{type(error).__name__}: {error}")

    def _write(self, table, rows: list[dict]) -> None:
        if not rows:
            return
        statement = mysql_insert(table).values(rows)
        # 幂等：进程重启后同一个桶重复写入只更新极值，不产生重复行。
        session_values = {
            "bid_low": statement.inserted.bid_low,
            "bid_high": statement.inserted.bid_high,
            "ask_low": statement.inserted.ask_low,
            "ask_high": statement.inserted.ask_high,
            "bid_close": statement.inserted.bid_close,
            "ask_close": statement.inserted.ask_close,
            "frame_count": statement.inserted.frame_count,
        }
        if table is MarketQuoteMinute:
            session_values["covered_seconds"] = statement.inserted.covered_seconds
        with Session(self.engine) as session:
            session.execute(statement.on_duplicate_key_update(**session_values))
            session.commit()

    def flush(self, now: datetime | None = None) -> tuple[int, int]:
        """把走完的秒桶与分钟桶落盘，返回写入行数。"""
        now = now or datetime.now(UTC)
        drained = self.drain_seconds(now)
        seconds = [
            {
                "instrument_id": self._instrument_ids[(venue, contract)],
                "bucket_time": bucket_time,
                "bid_low": bucket.bid_low, "bid_high": bucket.bid_high,
                "ask_low": bucket.ask_low, "ask_high": bucket.ask_high,
                "bid_close": bucket.bid_close, "ask_close": bucket.ask_close,
                "frame_count": bucket.frame_count,
            }
            for venue, contract, bucket_time, bucket in drained
        ]
        self._publish_snapshots(drained)
        minutes = [
            {
                "instrument_id": self._instrument_ids[(venue, contract)],
                "bucket_time": bucket_time,
                "bid_low": bucket.bid_low, "bid_high": bucket.bid_high,
                "ask_low": bucket.ask_low, "ask_high": bucket.ask_high,
                "bid_close": bucket.bid_close, "ask_close": bucket.ask_close,
                "frame_count": bucket.frame_count, "covered_seconds": covered,
            }
            for venue, contract, bucket_time, bucket, covered in self.drain_minutes(now)
        ]
        self._write(MarketQuoteSecond, seconds)
        self._write(MarketQuoteMinute, minutes)
        return len(seconds), len(minutes)

    async def _binance_stream(self) -> None:
        streams = "/".join(
            f"{INTERNAL_TO_BINANCE[c].lower()}@bookTicker" for c in self.contracts
        )
        reverse = {INTERNAL_TO_BINANCE[c]: c for c in self.contracts}
        await self._stream_loop(
            "币安", BINANCE_WS_URL + streams, None,
            lambda payload: self._on_binance(payload, reverse),
        )

    def _on_binance(self, payload: dict, reverse: dict) -> None:
        data = payload.get("data") or payload
        symbol = data.get("s")
        if symbol in reverse and "b" in data:
            bid, ask = float(data["b"]), float(data["a"])
            contract = reverse[symbol]
            self.record(BINANCE_LIVE_VENUE, contract, bid, ask)
            # E 是币安的事件时间（毫秒）。本机时钟和交易所有偏差，跨代理更明显，
            # 用本机时钟归属分钟会让K线边界错位。
            self._track_provisional(BINANCE_LIVE_VENUE, contract, bid, ask, data.get("E"))

    async def _gate_stream(self) -> None:
        subscribe = [
            {
                "time": int(time.time()), "channel": "futures.book_ticker",
                "event": "subscribe", "payload": [contract],
            }
            for contract in self.contracts
        ]
        await self._stream_loop("Gate", GATE_WS_URL, subscribe, self._on_gate)

    def _on_gate(self, payload: dict) -> None:
        if payload.get("event") != "update":
            return
        result = payload.get("result") or {}
        contract = result.get("s")
        if contract in self.contracts and "b" in result:
            bid, ask = float(result["b"]), float(result["a"])
            self.record(GATE_LIVE_VENUE, contract, bid, ask)
            # t 是 Gate 的推送时间（毫秒）；理由同币安。
            self._track_provisional(GATE_LIVE_VENUE, contract, bid, ask, result.get("t"))

    async def _stream_loop(
        self, label: str, url: str, subscribe: list[dict] | None, handle
    ) -> None:
        """一条连接的生命周期：连上、订阅、收帧；断开就退避重连。

        断线期间的盘口无法事后补齐——交易所不提供盘口历史。缺失会体现为
        分钟行的 covered_seconds 小于60，回测时据此排除不完整的分钟。
        """
        attempt = 0
        while not self._stopping.is_set():
            try:
                async with websockets.connect(url, open_timeout=20) as socket:
                    attempt = 0
                    self.reporter(f"{label}盘口已连接")
                    for message in subscribe or []:
                        await socket.send(json.dumps(message))
                    while not self._stopping.is_set():
                        raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 任何异常都只触发重连
                if self._stopping.is_set():
                    return
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                self.reporter(
                    f"{label}盘口断开（{type(error).__name__}: {str(error)[:60]}），"
                    f"{delay:.0f}秒后重连"
                )
                await asyncio.sleep(delay)

    async def _flush_loop(self) -> None:
        """每秒落盘一次；写库放到线程里，避免阻塞收帧的事件循环。"""
        while not self._stopping.is_set():
            await asyncio.sleep(SECOND_FLUSH_INTERVAL)
            try:
                await asyncio.to_thread(self.flush)
            except Exception as error:  # noqa: BLE001 - 落盘失败不能中断采集
                self.reporter(f"盘口落盘失败：{type(error).__name__}: {error}")

    def _bootstrap(self) -> None:
        """启动时用 MySQL 灌满 Redis，并把当前这一分钟已经过去的部分补回来。

        不走 REST：同样的数据 MySQL 里都有，本机读15个序列约440毫秒，而30个REST
        请求即使并发也要2秒，还白占限流额度。

        在途K线也要补：10:35:40 重启时，10:35 这根的前40秒没收到，open 和最高最低
        都是错的。秒级表里有那40秒。
        """
        for venue in (GATE_LIVE_VENUE, BINANCE_LIVE_VENUE):
            try:
                cursor = bootstrap_from_mysql(
                    venue, self.contracts, PUBLIC_INTERVALS, reporter=self.reporter
                )
                self.reporter(f"{venue} 灌载 {len(cursor)} 个K线序列进 Redis")
            except Exception as error:  # noqa: BLE001 - 灌载失败仍可继续采集盘口
                self.reporter(f"{venue} 灌载失败：{type(error).__name__}: {error}")
            for contract in self.contracts:
                try:
                    partial = rebuild_from_seconds(venue, contract, engine=self.engine)
                except Exception as error:  # noqa: BLE001
                    self.reporter(f"{venue} {contract} 在途K线补齐失败：{error}")
                    continue
                if partial is not None:
                    self._provisional.seed(venue, contract, partial)

    async def _provisional_loop(self) -> None:
        """每秒把在途K线推给 Redis。"""
        while not self._stopping.is_set():
            await asyncio.sleep(PROVISIONAL_PUBLISH_INTERVAL)
            try:
                await asyncio.to_thread(self._publish_provisional)
            except Exception as error:  # noqa: BLE001 - 不能中断采集
                self.reporter(f"在途K线循环异常：{type(error).__name__}: {error}")

    async def _bar_refresh_loop(self) -> None:
        """在每个整分边界之后拉一次到期周期的K线。

        对齐边界而不是固定节拍：固定2秒一跳平均要多等1秒才发现收线，而这段延迟
        直接加在"收线到策略做出判断"的链路上。所有周期都对齐UTC整分，因此每分钟
        醒一次就覆盖了全部。
        """
        while not self._stopping.is_set():
            now = datetime.now(UTC)
            nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            delay = (nxt - now).total_seconds() + BAR_CLOSE_SETTLE_SECONDS
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(self.refresh_bars)
            except Exception as error:  # noqa: BLE001 - 拉取失败不能中断盘口采集
                self.reporter(f"K线刷新失败：{type(error).__name__}: {error}")

    def request_stop(self) -> None:
        """请求停止；由信号处理器调用，实际收尾在 run() 的 finally 里完成。"""
        self.reporter("收到停止信号，正在冲刷内存中的桶")
        self._stopping.set()

    async def run(self, *, seconds: float | None = None) -> None:
        """启动采集；seconds 仅供测试限定运行时长，正式运行传 None 表示常驻。

        SIGTERM 与 SIGINT 都要能优雅停止：关闭 Terminal、kill、系统重启发的都是
        SIGTERM，不接管的话进程直接退出，内存里尚未落盘的秒桶与分钟桶会全部丢失，
        而盘口数据交易所不提供历史下载，丢了无法重建。
        """
        loop = asyncio.get_running_loop()
        for received in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(received, self.request_stop)
            except (NotImplementedError, RuntimeError):
                # 某些平台或非主线程不支持；此时仍可由调用方显式停止。
                pass
        self.load_instruments()
        self.reporter(f"盘口采集启动：{'、'.join(self.contracts)}，Gate与币安各一条连接")
        stack = ExitStack()
        self._gate_client = stack.enter_context(GatePublicClient())
        self._binance_client = stack.enter_context(BinancePublicClient())
        self._bootstrap()
        tasks = [
            asyncio.create_task(self._binance_stream()),
            asyncio.create_task(self._gate_stream()),
            asyncio.create_task(self._flush_loop()),
            asyncio.create_task(self._provisional_loop()),
            asyncio.create_task(self._bar_refresh_loop()),
        ]
        try:
            if seconds is None:
                await self._stopping.wait()
            else:
                await asyncio.sleep(seconds)
        finally:
            self._stopping.set()
            for task in tasks:
                task.cancel()
            # WebSocket 的关闭握手会等待对端回帧，连接已经不健康时可能一直等下去。
            # 落盘比优雅关闭重要得多，因此给收尾设上限，超时就直接往下走。
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=3.0
                )
            except TimeoutError:
                self.reporter("连接关闭超时，直接落盘")
            # 收尾时把内存里剩下的桶全部落盘，用未来时间强制冲刷。
            self.flush(datetime.now(UTC) + timedelta(minutes=2))
            stack.close()
            self.reporter("盘口采集已停止")
