"""Gate与币安实盘公共行情双影子账户七天对照服务。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from gold_crypto_quant.config import Settings
from gold_crypto_quant.exchanges.binance import BinancePublicClient
from gold_crypto_quant.exchanges.gate import GatePublicClient
from gold_crypto_quant.market_data.binance_history import (
    BINANCE_LIVE_VENUE,
    import_binance_history,
)
from gold_crypto_quant.market_data.gate_history import GATE_LIVE_VENUE, import_gate_history
from gold_crypto_quant.notifications.runtime_events import RuntimeEventNotifier
from gold_crypto_quant.runtime.bollinger_signal_cycle import (
    BollingerSignalCycleSummary,
    run_bollinger_signal_cycle,
)
from gold_crypto_quant.storage.market_health import refresh_market_health
from gold_crypto_quant.storage.shadow_monitor import record_shadow_cycle

PUBLIC_COMPARISON_CONTRACTS = ("BTC_USDT", "ETH_USDT")
PUBLIC_COMPARISON_INTERVALS = ("1m", "5m", "15m", "30m", "1h")
GATE_LIVE_STATE_PATH = Path(".runtime/bollinger-gate-live-paper-v5.json")
BINANCE_LIVE_STATE_PATH = Path(".runtime/bollinger-binance-live-paper-v5.json")


@dataclass(frozen=True, slots=True)
class ComparisonFeedResult:
    """单个交易所一轮行情和影子策略结果。"""

    label: str
    imported_bars: int
    summary: BollingerSignalCycleSummary


class PublicMarketComparisonRunner:
    """只读取两个交易所的实盘公共行情，维护相互隔离的本地影子账户。"""

    def __init__(
        self,
        settings: Settings,
        *,
        poll_seconds: float = 60.0,
        limit: int = 500,
        max_cycles: int = 10_080,
        stop_event: Event | None = None,
        reporter: Callable[[str], None] | None = None,
    ) -> None:
        if poll_seconds <= 0 or not 30 <= limit <= 1500 or max_cycles < 1:
            raise ValueError("invalid public comparison runner settings")
        self.poll_seconds = poll_seconds
        self.limit = limit
        self.max_cycles = max_cycles
        self.stop_event = stop_event or Event()
        self.reporter = reporter or (lambda _message: None)
        self.notifier = RuntimeEventNotifier(settings)
        self._failed_feeds: set[str] = set()

    def _notify(self, key: str, title: str, lines: tuple[str, ...], severity: str) -> None:
        """发送带对照服务前缀的事件；邮件失败不改变影子账户。"""
        self.notifier.send(
            event_key=f"public-comparison:{key}",
            event_title=f"双行情对照：{title}",
            event_lines=lines,
            severity=severity,
            repeatable=True,
            comparison_status_lines=(
                "运行模式：Gate＋币安双行情对照",
                "账户关系：同策略、同初始资金、独立行情、独立仓位",
                "真实交易：False",
                "交易所订单提交：False",
            ),
        )

    def _report_feed_failure(self, label: str, error: Exception) -> None:
        """行情首次中断立即告警；持续失败不按分钟轰炸邮箱。"""
        self.reporter(f"{label}实盘公共行情失败：{type(error).__name__}: {error}")
        if label in self._failed_feeds:
            return
        self._failed_feeds.add(label)
        self.notifier.send(
            event_key=f"public-comparison:{label}:feed-failed",
            event_title=f"[{label}] 公共行情中断",
            event_lines=(
                f"交易所：{label}",
                f"异常类型：{type(error).__name__}",
                f"异常信息：{error}",
                "处理方式：暂停该交易所本轮信号，另一交易所继续运行",
            ),
            severity="CRITICAL",
            comparison_status_lines=(
                f"{label}行情：INTERRUPTED",
                "另一行情源：继续独立运行",
                "真实交易：False",
                "交易所订单提交：False",
            ),
        )

    def _report_feed_recovered(self, label: str) -> None:
        """中断后的首个成功周期发送恢复邮件。"""
        if label not in self._failed_feeds:
            return
        self._failed_feeds.remove(label)
        self.notifier.send(
            event_key=f"public-comparison:{label}:feed-recovered:{datetime.now(UTC).isoformat()}",
            event_title=f"[{label}] 公共行情恢复",
            event_lines=(f"交易所：{label}", "状态：行情刷新和影子策略已恢复"),
            severity="RECOVERED",
            repeatable=True,
            comparison_status_lines=(
                f"{label}行情：HEALTHY",
                "双行情对照：继续运行",
                "真实交易：False",
                "交易所订单提交：False",
            ),
        )

    def _refresh_health(self, venue: str) -> None:
        """在策略运行前确认该交易所全部公开行情流新鲜。"""
        for contract in PUBLIC_COMPARISON_CONTRACTS:
            for interval in PUBLIC_COMPARISON_INTERVALS:
                refresh_market_health(contract, interval, venue=venue)

    def _report_strategy_events(
        self,
        label: str,
        venue: str,
        summary: BollingerSignalCycleSummary,
        comparison_status_lines: tuple[str, ...],
    ) -> None:
        """交易邮件明确标出行情来源，避免和原Gate测试网影子账户混淆。"""
        for event in summary.paper_events:
            self.notifier.send(
                event_key=f"public-comparison:{venue}:{event.event_key}",
                event_title=f"[{label}] {event.title}",
                event_lines=(
                    f"交易所：{label}",
                    f"账户：{label}独立影子账户",
                    f"行情来源：{label}实盘公共行情",
                    *event.lines,
                ),
                severity=event.severity,
                repeatable=True,
                venue=venue,
                comparison_status_lines=comparison_status_lines,
            )

    @staticmethod
    def _comparison_status_lines(
        feed_results: list[ComparisonFeedResult],
    ) -> tuple[str, ...]:
        """把本轮两套账户压缩成适合手机查看的对照摘要。"""
        by_label = {item.label: item for item in feed_results}

        def account_line(label: str) -> str:
            item = by_label.get(label)
            if item is None:
                return f"{label}影子账户：本轮行情异常"
            return (
                f"{label}影子账户：权益 {item.summary.paper_equity:.2f}U / "
                f"状态 {item.summary.paper_status} / 新信号 {item.summary.new_signal_count}"
            )

        gate = by_label.get("Gate")
        binance = by_label.get("币安")
        difference = (
            f"权益差（Gate-币安）：{gate.summary.paper_equity - binance.summary.paper_equity:+.2f}U"
            if gate is not None and binance is not None
            else "权益差（Gate-币安）：本轮不可计算"
        )
        return (
            account_line("Gate"),
            account_line("币安"),
            difference,
            "对照规则：同策略、同初始资金、各自行情、账户互不影响",
            "真实交易：False",
            "交易所订单提交：False",
        )

    def run(self) -> int:
        """运行最多七天；任一交易所短暂失败不会停止另一套影子账户。"""
        completed_cycles = 0
        self._notify(
            "started",
            "服务启动",
            ("Gate实盘公共行情：启用", "币安实盘公共行情：启用", "API密钥：不需要"),
            "INFO",
        )
        with GatePublicClient() as gate, BinancePublicClient() as binance:
            while not self.stop_event.is_set() and completed_cycles < self.max_cycles:
                cycle_started = datetime.now(UTC)
                feed_results: list[ComparisonFeedResult] = []
                try:
                    gate_imports = import_gate_history(
                        gate,
                        contracts=PUBLIC_COMPARISON_CONTRACTS,
                        intervals=PUBLIC_COMPARISON_INTERVALS,
                        limit=self.limit,
                        venue=GATE_LIVE_VENUE,
                    )
                    self._refresh_health(GATE_LIVE_VENUE)
                    gate_summary = run_bollinger_signal_cycle(
                        venue=GATE_LIVE_VENUE,
                        state_path=GATE_LIVE_STATE_PATH,
                    )
                    feed_results.append(
                        ComparisonFeedResult(
                            "Gate",
                            sum(item.stored for item in gate_imports),
                            gate_summary,
                        )
                    )
                    # 保存独立Gate影子权益与交易事件，Web后台只读这些监管数据。
                    record_shadow_cycle(GATE_LIVE_VENUE, GATE_LIVE_STATE_PATH, gate_summary)
                    self._report_feed_recovered("Gate")
                except Exception as error:
                    self._report_feed_failure("Gate", error)

                try:
                    binance_imports = import_binance_history(
                        binance,
                        contracts=PUBLIC_COMPARISON_CONTRACTS,
                        intervals=PUBLIC_COMPARISON_INTERVALS,
                        limit=self.limit,
                    )
                    self._refresh_health(BINANCE_LIVE_VENUE)
                    binance_summary = run_bollinger_signal_cycle(
                        venue=BINANCE_LIVE_VENUE,
                        state_path=BINANCE_LIVE_STATE_PATH,
                    )
                    feed_results.append(
                        ComparisonFeedResult(
                            "币安",
                            sum(item.stored for item in binance_imports),
                            binance_summary,
                        )
                    )
                    # 保存独立币安影子权益与交易事件，账户和Gate完全隔离。
                    record_shadow_cycle(
                        BINANCE_LIVE_VENUE,
                        BINANCE_LIVE_STATE_PATH,
                        binance_summary,
                    )
                    self._report_feed_recovered("币安")
                except Exception as error:
                    self._report_feed_failure("币安", error)

                completed_cycles += 1
                comparison_status_lines = self._comparison_status_lines(feed_results)
                for item in feed_results:
                    venue = GATE_LIVE_VENUE if item.label == "Gate" else BINANCE_LIVE_VENUE
                    self._report_strategy_events(
                        item.label,
                        venue,
                        item.summary,
                        comparison_status_lines,
                    )
                if feed_results:
                    details = "；".join(
                        f"{item.label}刷新{item.imported_bars}根、权益"
                        f"{item.summary.paper_equity:.2f}U、信号{item.summary.new_signal_count}条"
                        for item in feed_results
                    )
                    self.reporter(f"双行情对照第{completed_cycles}轮完成：{details}")
                else:
                    self.reporter(f"双行情对照第{completed_cycles}轮：两个行情源均失败")
                elapsed = (datetime.now(UTC) - cycle_started).total_seconds()
                self.stop_event.wait(max(0.1, self.poll_seconds - elapsed))

        self._notify(
            "stopped",
            "服务停止",
            (f"成功调度轮数：{completed_cycles}",),
            "WARNING",
        )
        return completed_cycles
