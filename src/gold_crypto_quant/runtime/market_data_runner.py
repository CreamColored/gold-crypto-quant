"""Gate测试网行情长期轮询、断线重连和安全退出。"""

import fcntl
import os
import re
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Any

from gold_crypto_quant.config import Settings
from gold_crypto_quant.exchanges.gate import GateTestnetClient
from gold_crypto_quant.market_data.gate_history import ImportResult, import_gate_history
from gold_crypto_quant.storage.market_health import refresh_market_health
from gold_crypto_quant.storage.service_state import save_service_state

MARKET_DATA_SERVICE_NAME = "GATE_TESTNET_MARKET_DATA"
OANDA_MARKET_DATA_SERVICE_NAME = "OANDA_PRACTICE_MARKET_DATA"
_SECRET_FIELD_PATTERN = re.compile(r"(?i)(api[_-]?key|secret|password|token)(\s*[:=]\s*)([^\s,;]+)")
_DATABASE_CREDENTIAL_PATTERN = re.compile(r"://[^:@/\s]+:[^@/\s]+@")


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """行情轮询服务的受控运行参数。"""

    contracts: tuple[str, ...] = ("BTC_USDT", "ETH_USDT")
    intervals: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h")
    limit: int = 500
    poll_seconds: float = 60.0
    retry_initial_seconds: float = 5.0
    retry_max_seconds: float = 300.0
    max_cycles: int | None = None

    def __post_init__(self) -> None:
        """在启动网络和数据库操作前拒绝无效调度参数。"""
        if not self.contracts or not self.intervals:
            raise ValueError("runner contracts and intervals cannot be empty")
        if not 1 <= self.limit <= 2000:
            raise ValueError("runner limit must be in [1, 2000]")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.retry_initial_seconds <= 0:
            raise ValueError("retry_initial_seconds must be positive")
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("retry_max_seconds cannot be smaller than retry_initial_seconds")
        if self.max_cycles is not None and self.max_cycles < 1:
            raise ValueError("max_cycles must be positive when provided")


@dataclass(frozen=True, slots=True)
class RunnerResult:
    """服务本次启动直到退出的汇总结果。"""

    successful_cycles: int
    stopped_by_request: bool


def retry_delay_seconds(
    consecutive_failures: int,
    *,
    initial_seconds: float,
    maximum_seconds: float,
) -> float:
    """计算封顶指数退避时间，第一次失败使用初始等待值。"""
    if consecutive_failures < 1:
        raise ValueError("consecutive_failures must be at least one")
    return min(initial_seconds * (2 ** (consecutive_failures - 1)), maximum_seconds)


def safe_error_summary(error: Exception) -> str:
    """生成可排障但不应泄漏密码、令牌或API密钥的异常摘要。"""
    summary = f"{type(error).__name__}: {error}"
    summary = _DATABASE_CREDENTIAL_PATTERN.sub("://***:***@", summary)
    summary = _SECRET_FIELD_PATTERN.sub(r"\1\2***", summary)
    return summary[:1000]


class SingleInstanceLock:
    """使用操作系统文件锁防止同一台机器重复启动行情服务。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def __enter__(self) -> "SingleInstanceLock":
        """非阻塞获取独占锁，并把当前PID写入锁文件。"""
        # 调用mkdir创建仅用于运行状态的目录；已存在时不会清空任何文件。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # 调用flock做非阻塞独占锁，第二个进程会立即失败而不是同时采集。
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError(f"market data runner is already active: {self.path}") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        self._descriptor = descriptor
        return self

    def __exit__(self, *_: object) -> None:
        """释放锁；保留小型锁文件便于排查最近一次进程ID。"""
        if self._descriptor is None:
            return
        # 调用flock显式解锁后关闭文件描述符，确保其他进程可以立即接管。
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = None


@contextmanager
def install_shutdown_signal_handlers(stop_event: Event) -> Iterator[None]:
    """把SIGINT和SIGTERM转换为可中断等待的安全停止请求。"""
    previous_handlers: dict[signal.Signals, Any] = {}

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        # 调用Event.set只发出停止请求，当前数据库事务和HTTP请求仍可正常收尾。
        stop_event.set()

    for supported_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[supported_signal] = signal.getsignal(supported_signal)
        signal.signal(supported_signal, request_stop)
    try:
        yield
    finally:
        for supported_signal, previous_handler in previous_handlers.items():
            # 调用signal恢复调用方原有处理器，避免测试或嵌入式运行污染全局状态。
            signal.signal(supported_signal, previous_handler)


class MarketDataRunner:
    """持续刷新K线和行情健康状态，失败时重建客户端并指数退避。"""

    def __init__(
        self,
        settings: Settings,
        config: RunnerConfig,
        *,
        stop_event: Event | None = None,
        client_factory: Callable[[], GateTestnetClient] | None = None,
        importer: Callable[..., list[ImportResult]] = import_gate_history,
        health_refresher: Callable[..., Any] = refresh_market_health,
        state_saver: Callable[..., Any] = save_service_state,
        after_cycle: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        reporter: Callable[[str], None] | None = None,
        event_reporter: Callable[[str, str], None] | None = None,
        service_name: str = MARKET_DATA_SERVICE_NAME,
    ) -> None:
        self.settings = settings
        self.config = config
        self.stop_event = stop_event or Event()
        self.client_factory = client_factory or (
            lambda: GateTestnetClient.from_settings(self.settings)
        )
        self.importer = importer
        self.health_refresher = health_refresher
        self.state_saver = state_saver
        self.after_cycle = after_cycle
        self.clock = clock or (lambda: datetime.now(UTC))
        self.reporter = reporter or (lambda _message: None)
        self.event_reporter = event_reporter or (lambda _event, _detail: None)
        self.service_name = service_name

    def _report_event(self, event: str, detail: str) -> None:
        """调用旁路事件通知；邮件等通知故障不能拖垮行情主循环。"""
        try:
            self.event_reporter(event, detail)
        except Exception as error:
            self.reporter(f"事件通知失败：{safe_error_summary(error)}")

    def _save_state(
        self,
        status: str,
        *,
        started_at: datetime,
        successful_cycles: int,
        consecutive_failures: int,
        stopped_at: datetime | None = None,
        last_error: str | None = None,
    ) -> None:
        """保存服务心跳；状态库暂时不可用时不妨碍后续重连。"""
        try:
            # 调用幂等状态保存，让运维界面始终读取同一条服务记录。
            self.state_saver(
                service_name=self.service_name,
                status=status,
                process_id=os.getpid(),
                started_at=started_at,
                heartbeat_at=self.clock(),
                stopped_at=stopped_at,
                successful_cycles=successful_cycles,
                consecutive_failures=consecutive_failures,
                last_error=last_error,
                details={
                    "contracts": list(self.config.contracts),
                    "intervals": list(self.config.intervals),
                    "poll_seconds": self.config.poll_seconds,
                    "live_trading": False,
                },
            )
        except Exception as error:  # 状态库故障必须被下一轮数据导入重试覆盖。
            self.reporter(f"服务状态保存失败：{safe_error_summary(error)}")

    def _run_cycle(self) -> int:
        """执行一轮行情更新并返回写入或刷新过的已收盘K线数量。"""
        # 每轮调用工厂重新建立客户端；with结束时关闭连接，失败后的下一轮天然重连。
        with self.client_factory() as client:
            # 调用现有幂等导入方法，重复拉取最近K线不会生成重复数据库记录。
            results = self.importer(
                client,
                contracts=self.config.contracts,
                intervals=self.config.intervals,
                limit=self.config.limit,
            )
        for contract in self.config.contracts:
            for interval in self.config.intervals:
                # 仅在整轮网络导入成功后调用健康检查，避免失败请求伪造新心跳。
                self.health_refresher(contract, interval)
        if self.after_cycle is not None:
            # 行情和全部健康状态完成后才调用扩展周期，避免策略读取半更新的数据快照。
            self.after_cycle()
        return sum(result.stored for result in results)

    def run(self) -> RunnerResult:
        """运行到收到停止信号，或达到仅用于验证的最大成功轮数。"""
        started_at = self.clock()
        successful_cycles = 0
        consecutive_failures = 0
        # 启动网络操作前调用状态保存，外部可以区分刚启动和已完成首轮。
        self._save_state(
            "STARTING",
            started_at=started_at,
            successful_cycles=successful_cycles,
            consecutive_failures=consecutive_failures,
        )
        self._report_event("SERVICE_STARTED", "Gate行情观察服务已经启动")
        try:
            while not self.stop_event.is_set():
                try:
                    updated_bars = self._run_cycle()
                except Exception as error:
                    consecutive_failures += 1
                    error_summary = safe_error_summary(error)
                    delay = retry_delay_seconds(
                        consecutive_failures,
                        initial_seconds=self.config.retry_initial_seconds,
                        maximum_seconds=self.config.retry_max_seconds,
                    )
                    self.reporter(
                        f"行情轮询失败（连续{consecutive_failures}次），"
                        f"{delay:g}秒后重试：{error_summary}"
                    )
                    if consecutive_failures == 1:
                        # 第一次失败立即发异常事件；连续重试只更新日志，避免每轮重复邮件。
                        self._report_event("SERVICE_RETRYING", error_summary)
                    # 调用状态保存记录RETRYING；任何订单会继续受旧行情心跳超时门禁保护。
                    self._save_state(
                        "RETRYING",
                        started_at=started_at,
                        successful_cycles=successful_cycles,
                        consecutive_failures=consecutive_failures,
                        last_error=error_summary,
                    )
                    # Event.wait可被SIGINT或SIGTERM立即唤醒，不会卡在不可中断的sleep中。
                    self.stop_event.wait(delay)
                    continue

                recovered_failures = consecutive_failures
                successful_cycles += 1
                consecutive_failures = 0
                if recovered_failures > 0:
                    # 调用恢复事件通知，让用户知道此前异常已经结束。
                    self._report_event(
                        "SERVICE_RECOVERED",
                        f"行情服务在连续失败{recovered_failures}次后恢复",
                    )
                self.reporter(f"行情轮询第{successful_cycles}轮完成，刷新{updated_bars}条已收盘K线")
                # 成功后调用状态保存清零连续失败和最近错误，发布本轮服务心跳。
                self._save_state(
                    "RUNNING",
                    started_at=started_at,
                    successful_cycles=successful_cycles,
                    consecutive_failures=consecutive_failures,
                )
                if (
                    self.config.max_cycles is not None
                    and successful_cycles >= self.config.max_cycles
                ):
                    break
                # 调用可中断等待控制轮询频率，停止信号无需等待完整周期。
                self.stop_event.wait(self.config.poll_seconds)
        finally:
            stopped_at = self.clock()
            # 无论正常停止、Ctrl+C还是意外异常，都尽力保存最终STOPPED状态。
            self._save_state(
                "STOPPED",
                started_at=started_at,
                successful_cycles=successful_cycles,
                consecutive_failures=consecutive_failures,
                stopped_at=stopped_at,
            )
            self._report_event("SERVICE_STOPPED", "Gate行情观察服务已经停止")
        return RunnerResult(
            successful_cycles=successful_cycles,
            stopped_by_request=self.stop_event.is_set(),
        )
