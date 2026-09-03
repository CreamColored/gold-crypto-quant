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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import websockets
from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.market_data.binance_history import BINANCE_LIVE_VENUE, INTERNAL_TO_BINANCE
from gold_crypto_quant.market_data.gate_history import GATE_LIVE_VENUE
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, MarketQuoteMinute, MarketQuoteSecond

QUOTE_CONTRACTS = ("BTC_USDT", "ETH_USDT", "XAU_USDT")
BINANCE_WS_URL = "wss://fstream.binance.com/stream?streams="
GATE_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
# 断线重连退避；上限不宜太大，盘口断开期间无法事后补齐。
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 20.0)
SECOND_FLUSH_INTERVAL = 1.0


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

    @classmethod
    def start(cls, bid: float, ask: float) -> "QuoteBucket":
        return cls(bid, bid, ask, ask, 1)

    def fold(self, bid: float, ask: float) -> None:
        """并入一帧盘口。"""
        self.bid_low = min(self.bid_low, bid)
        self.bid_high = max(self.bid_high, bid)
        self.ask_low = min(self.ask_low, ask)
        self.ask_high = max(self.ask_high, ask)
        self.frame_count += 1

    def merge(self, other: "QuoteBucket") -> None:
        """并入另一个桶；秒级汇总成分钟级时使用，结果与直接聚合原始帧完全一致。"""
        self.bid_low = min(self.bid_low, other.bid_low)
        self.bid_high = max(self.bid_high, other.bid_high)
        self.ask_low = min(self.ask_low, other.ask_low)
        self.ask_high = max(self.ask_high, other.ask_high)
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
        seconds = [
            {
                "instrument_id": self._instrument_ids[(venue, contract)],
                "bucket_time": bucket_time,
                "bid_low": bucket.bid_low, "bid_high": bucket.bid_high,
                "ask_low": bucket.ask_low, "ask_high": bucket.ask_high,
                "frame_count": bucket.frame_count,
            }
            for venue, contract, bucket_time, bucket in self.drain_seconds(now)
        ]
        minutes = [
            {
                "instrument_id": self._instrument_ids[(venue, contract)],
                "bucket_time": bucket_time,
                "bid_low": bucket.bid_low, "bid_high": bucket.bid_high,
                "ask_low": bucket.ask_low, "ask_high": bucket.ask_high,
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
            self.record(BINANCE_LIVE_VENUE, reverse[symbol], float(data["b"]), float(data["a"]))

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
            self.record(GATE_LIVE_VENUE, contract, float(result["b"]), float(result["a"]))

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
        tasks = [
            asyncio.create_task(self._binance_stream()),
            asyncio.create_task(self._gate_stream()),
            asyncio.create_task(self._flush_loop()),
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
            self.reporter("盘口采集已停止")
