"""Gate与币安实盘公共行情双影子账户七天对照服务。"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

from gold_crypto_quant.config import Settings
from gold_crypto_quant.market_data.binance_history import (
    BINANCE_LIVE_VENUE,
)
from gold_crypto_quant.market_data.gate_history import (
    GATE_LIVE_VENUE,
    take_deadlock_retry_count,
)
from gold_crypto_quant.notifications.runtime_events import RuntimeEventNotifier
from gold_crypto_quant.runtime.bollinger_signal_cycle import (
    BollingerSignalCycleSummary,
    run_bollinger_signal_cycle,
)
from gold_crypto_quant.storage.bar_source import SourceHealth, check_health
from gold_crypto_quant.storage.market_health import refresh_market_health
from gold_crypto_quant.storage.redis_bars import read_cursor
from gold_crypto_quant.storage.shadow_monitor import record_shadow_cycle

PUBLIC_COMPARISON_CONTRACTS = ("BTC_USDT", "ETH_USDT", "XAU_USDT")
PUBLIC_COMPARISON_INTERVALS = ("1m", "5m", "15m", "30m", "1h")
# 单轮超过这个秒数才算节奏丢失——策略按收线K线推进，跨过整根才会漏掉判定。
CYCLE_OVERRUN_SECONDS = 30.0
GATE_LIVE_STATE_PATH = Path(".runtime/bollinger-gate-live-paper-v5.json")
BINANCE_LIVE_STATE_PATH = Path(".runtime/bollinger-binance-live-paper-v5.json")
# 单轮耗时超过轮询间隔时的重复告警间隔；持续超时按这个周期节流，不逐轮刷屏。
CYCLE_OVERRUN_ALERT_COOLDOWN = timedelta(minutes=15)
# 连续超时达到这个轮数才告警；偶发一轮变慢（网络抖动、交易所响应慢）不值得打扰。
CYCLE_OVERRUN_ALERT_STREAK = 3
# 行情源连续失败达到这个轮数才告警。按20秒轮询算约6.7分钟——交易所和代理的
# 短暂抖动（2026-09-03下午币安那次503持续3分钟）不值得打扰，而且抖动期间
# 失败与成功交替出现，按"状态翻转"告警会连发好几对中断与恢复。
FEED_FAILURE_ALERT_STREAK = 20


@dataclass(slots=True)
class FeedOutageWatch:
    """跟踪单个行情源的连续失败，决定何时告警、何时报恢复。

    与 CycleDurationWatch 同一套思路：判定抽成不碰I/O的纯逻辑，便于独立测试。
    关键是 alerted 这个标志——它保证一次故障只发一条中断、一条恢复，
    而不是每次失败与成功的交替都发一对。
    """

    alert_after: int = FEED_FAILURE_ALERT_STREAK
    streak: int = 0
    alerted: bool = False

    def on_failure(self) -> bool:
        """记一次失败；返回本次是否应当发出中断告警。"""
        self.streak += 1
        if self.alerted or self.streak < self.alert_after:
            return False
        self.alerted = True
        return True

    def on_success(self) -> bool:
        """记一次成功；返回本次是否应当发出恢复通知。

        只有真的告过警才报恢复，否则会出现没报过故障却收到"已恢复"。
        """
        recovered = self.alerted
        self.streak = 0
        self.alerted = False
        return recovered


@dataclass(slots=True)
class CycleDurationWatch:
    """判断单轮耗时是否超出轮询间隔，并对持续超时做告警节流。

    单轮工作一旦超过 poll_seconds，循环里的 wait(max(0.1, poll_seconds - elapsed))
    就退化成只等0.1秒，服务表面正常、实际在背靠背空转——2026-09-03 之前它以119秒的
    间隔跑了好几天都没人发现。这里把判定抽成不碰I/O的纯逻辑，便于独立测试。
    """

    poll_seconds: float
    cooldown: timedelta = CYCLE_OVERRUN_ALERT_COOLDOWN
    alert_after: int = CYCLE_OVERRUN_ALERT_STREAK
    streak: int = 0
    last_alert: datetime | None = None

    def observe(self, elapsed: float, now: datetime) -> str:
        """返回本轮该发的通知：``overrun``、``recovered`` 或空串（不发）。"""
        if elapsed <= self.poll_seconds:
            # 只在本次超时确实告过警时才发恢复通知；否则会出现没报过故障却报恢复。
            recovered = self.last_alert is not None
            self.streak = 0
            self.last_alert = None
            return "recovered" if recovered else ""
        self.streak += 1
        # 偶发一两轮变慢不告警，连续超时才说明轮询节奏真的丢了。
        if self.streak < self.alert_after:
            return ""
        if self.last_alert is not None and now - self.last_alert < self.cooldown:
            return ""
        self.last_alert = now
        return "overrun"


@dataclass(frozen=True, slots=True)
class ComparisonFeedResult:
    """单个交易所一轮行情和影子策略结果。"""

    label: str
    # 本轮K线取自哪一边；redis 为常态，mysql 说明采集服务的视图已陈旧。
    source: str
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
        self.notifier = RuntimeEventNotifier(settings, reporter=self.reporter)
        self._feed_watches: dict[str, FeedOutageWatch] = {}
        # 阈值不再跟随 poll_seconds。1秒轮询下绝大多数轮次是空转，"单轮超过1秒"
        # 不代表节奏丢失；真正的风险是单轮长到跨过整根1分钟K线。
        self._duration_watch = CycleDurationWatch(CYCLE_OVERRUN_SECONDS)
        # 上一轮看到的收线水位。cursor 没变说明没有新K线，整轮跳过——1秒轮询下
        # 60轮里有59轮属于这种情况，跳过后单轮只花一次 HGETALL 约0.5毫秒。
        self._last_cursor: dict[str, dict[str, str]] = {
            GATE_LIVE_VENUE: {},
            BINANCE_LIVE_VENUE: {},
        }
        # 已经就降级发过告警的交易所，避免每秒重复推送。
        self._degraded: set[str] = set()

    def _notify(self, key: str, title: str, lines: tuple[str, ...], severity: str) -> None:
        """发送带对照服务前缀的事件；邮件失败不改变影子账户。"""
        self.notifier.send(
            event_key=f"public-comparison:{key}",
            event_title=f"量化服务：{title}",
            event_lines=lines,
            severity=severity,
            repeatable=True,
            comparison_status_lines=(
                "运行模式：Gate＋币安双所对照",
                "账户关系：同策略、同初始资金、独立行情、独立仓位",
                "真实交易：False",
                "交易所订单提交：False",
            ),
        )

    def _cursor_advanced(self, venue: str) -> tuple[bool, SourceHealth]:
        """本轮有没有新收线K线，以及 Redis 视图是否新鲜。

        每秒只读一个 Hash 就能回答"有没有新K线"，命中率约1/60；没变就不去读整份
        K线也不跑策略，59/60 的轮次因此只花约0.5毫秒。
        """
        health = check_health(venue)
        if health.degraded:
            # 降级期间无法用 cursor 判断，每轮都跑；MySQL 那条路径本来就是旧节奏。
            return True, health
        try:
            cursor = {k: v for k, v in read_cursor(venue).items() if k != "_heartbeat"}
        except Exception:  # noqa: BLE001 - 读不到就当有变化，宁可多跑一轮
            return True, health
        changed = cursor != self._last_cursor[venue]
        if changed:
            self._last_cursor[venue] = cursor
        return changed, health

    def _report_source(self, label: str, venue: str, health: SourceHealth) -> None:
        """降级和恢复各推一次，不按轮重复。"""
        if health.degraded and venue not in self._degraded:
            self._degraded.add(venue)
            self._notify(
                "source-degraded",
                f"{label}行情源降级",
                (f"原因：{health.reason}", "已切换为直接读取 MySQL，策略继续运行"),
                "WARNING",
            )
        elif not health.degraded and venue in self._degraded:
            self._degraded.discard(venue)
            self._notify(
                "source-recovered",
                f"{label}行情源恢复",
                ("采集服务心跳恢复，已切回 Redis",),
                "INFO",
            )

    def _run_feed(
        self,
        label: str,
        venue: str,
        state_path: Path,
        health: SourceHealth,
        *,
        bars_changed: bool = True,
    ) -> tuple:
        """跑一个交易所的策略流水线。行情拉取已搬到采集服务，这里只消费。

        两个交易所之间没有任何共享可变状态——不同的 instrument、不同的影子账户
        状态文件、不同的监管表行——因此可以并行。策略那一步在 venue 内部仍然串行：
        三个品种共享同一份 equity、跨品种顺序由 symbols.index() 定死、
        整份状态文件原子替换，按品种拆线程会直接产生竞态。
        """
        started = datetime.now(UTC)
        result: ComparisonFeedResult | None = None
        try:
            # 健康检查交给 run_bollinger_signal_cycle 内部去做——这里原来还有一份
            # 独立的 _refresh_health，两处做的是同一件事，等于每轮重复了一遍。
            summary = run_bollinger_signal_cycle(
                symbols=PUBLIC_COMPARISON_CONTRACTS,
                venue=venue,
                state_path=state_path,
                source_health=health,
                refresh_health=bars_changed,
            )
            result = ComparisonFeedResult(label, health.source, summary)
            # 保存该交易所独立的影子权益与交易事件，Web后台只读这些监管数据。
            # 快照表按分钟去重，1秒轮询下同一行会被反复 upsert 60 次，59 次是无效
            # 写入却各自是一个完整的 InnoDB 事务。只在有新收线K线或真的产生了事件
            # 时才写——前者保证每分钟至少一次快照，后者保证在途开仓不会漏记。
            if bars_changed or summary.paper_events:
                record_shadow_cycle(venue, state_path, summary)
            self._report_feed_recovered(label)
        except Exception as error:  # noqa: BLE001 - 一个交易所失败不能带停另一个
            self._report_feed_failure(label, error)
        return result, (datetime.now(UTC) - started).total_seconds()

    def _report_cycle_duration(self, elapsed: float) -> None:
        """单轮耗时超过轮询间隔即告警，持续超时按冷却期节流，回落后发恢复通知。"""
        action = self._duration_watch.observe(elapsed, datetime.now(UTC))
        streak = self._duration_watch.streak
        if elapsed > CYCLE_OVERRUN_SECONDS:
            # 无论告警是否被节流掉，日志每轮都留痕，便于事后回溯超时是从哪一轮开始的。
            self.reporter(
                f"单轮耗时{elapsed:.1f}秒，超过{CYCLE_OVERRUN_SECONDS:.0f}秒告警阈值"
                f"（连续第{streak}轮）"
            )
        if action == "overrun":
            self._notify(
                "cycle-overrun",
                "单轮耗时超过轮询间隔",
                (
                    f"本轮耗时：{elapsed:.1f} 秒",
                    f"轮询间隔：{self.poll_seconds:g} 秒，告警阈值 {CYCLE_OVERRUN_SECONDS:.0f} 秒",
                    f"连续超时轮数：{streak}（达到{CYCLE_OVERRUN_ALERT_STREAK}轮才告警）",
                    "影响：循环已退化成只等0.1秒，行情处理不再有固定节奏",
                    f"重复告警间隔：{CYCLE_OVERRUN_ALERT_COOLDOWN.total_seconds() / 60:.0f} 分钟",
                ),
                "WARNING",
            )
        elif action == "recovered":
            self._notify(
                "cycle-duration-recovered",
                "单轮耗时恢复正常",
                (
                    f"本轮耗时：{elapsed:.1f} 秒",
                    f"轮询间隔：{self.poll_seconds:g} 秒，告警阈值 {CYCLE_OVERRUN_SECONDS:.0f} 秒",
                ),
                "RECOVERED",
            )

    def _watch(self, label: str) -> FeedOutageWatch:
        return self._feed_watches.setdefault(label, FeedOutageWatch())

    def _report_feed_failure(self, label: str, error: Exception) -> None:
        """连续失败达到阈值才告警；每一轮失败都记日志，便于回溯故障起点。"""
        watch = self._watch(label)
        should_alert = watch.on_failure()
        self.reporter(
            f"{label}实盘公共行情失败（连续第{watch.streak}轮）："
            f"{type(error).__name__}: {error}"
        )
        if not should_alert:
            return
        self.notifier.send(
            event_key=f"public-comparison:{label}:feed-failed:{datetime.now(UTC).isoformat()}",
            event_title=f"[{label}] 公共行情中断",
            event_lines=(
                f"交易所：{label}",
                f"连续失败轮数：{watch.streak}（达到{FEED_FAILURE_ALERT_STREAK}轮才告警）",
                f"异常类型：{type(error).__name__}",
                f"异常信息：{error}",
                "处理方式：暂停该交易所本轮信号，另一交易所继续运行",
            ),
            severity="CRITICAL",
            repeatable=True,
            comparison_status_lines=(
                f"{label}行情：INTERRUPTED",
                "另一行情源：继续独立运行",
                "真实交易：False",
                "交易所订单提交：False",
            ),
        )

    def _report_feed_recovered(self, label: str) -> None:
        """只有真的发过中断告警才报恢复；闪断没告过警就不该有恢复通知。"""
        if not self._watch(label).on_success():
            return
        self.notifier.send(
            event_key=f"public-comparison:{label}:feed-recovered:{datetime.now(UTC).isoformat()}",
            event_title=f"[{label}] 公共行情恢复",
            event_lines=(f"交易所：{label}", "状态：行情刷新和影子策略已恢复"),
            severity="RECOVERED",
            repeatable=True,
            comparison_status_lines=(
                f"{label}行情：HEALTHY",
                "量化服务：继续运行",
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
        """逐笔策略事件只推钉钉；标出行情来源，避免和原Gate测试网影子账户混淆。"""
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
                category="TRADE",
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

        def holding_line(label: str) -> str:
            """列出该账户此刻在场的仓位；手机上一眼看清手里还有什么。"""
            item = by_label.get(label)
            if item is None:
                return f"{label}持仓：本轮行情异常"
            return f"{label}持仓：{item.summary.paper_holdings or '未知'}"

        gate = by_label.get("Gate")
        binance = by_label.get("币安")
        difference = (
            f"权益差（Gate-币安）：{gate.summary.paper_equity - binance.summary.paper_equity:+.2f}U"
            if gate is not None and binance is not None
            else "权益差（Gate-币安）：本轮不可计算"
        )
        return (
            account_line("Gate"),
            holding_line("Gate"),
            account_line("币安"),
            holding_line("币安"),
            difference,
            "对照规则：同策略、同初始资金、各自行情、账户互不影响",
            "真实交易：False",
            "交易所订单提交：False",
        )

    def run(self) -> int:
        """运行最多七天；任一交易所短暂失败不会停止另一套影子账户。"""
        completed_cycles = 0
        # 启停不再推送邮件和钉钉：重启是日常操作，每次都推会让真正的异常淹在里面。
        # 日志仍然留痕，行情中断、单轮超时、Redis降级这些异常告警照常发。
        self.reporter("量化服务启动：Gate与币安实盘公共行情，不需要API密钥")
        while not self.stop_event.is_set() and completed_cycles < self.max_cycles:
            cycle_started = datetime.now(UTC)
            # 每秒都跑：在途K线每秒变一次，触轨的停留确认要靠逐秒复查才能计时。
            # cursor 有没有前进只决定要不要刷健康表和写日志。
            plans = []
            bars_changed = False
            for label, venue, state_path in (
                ("Gate", GATE_LIVE_VENUE, GATE_LIVE_STATE_PATH),
                ("币安", BINANCE_LIVE_VENUE, BINANCE_LIVE_STATE_PATH),
            ):
                advanced, health = self._cursor_advanced(venue)
                self._report_source(label, venue, health)
                bars_changed = bars_changed or advanced
                plans.append((label, venue, state_path, health, advanced))

            gate_result = binance_result = None
            gate_seconds = binance_seconds = 0.0
            # 两个交易所之间没有共享可变状态，并行；策略在 venue 内部仍串行。
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="feed") as pool:
                futures = {
                    label: pool.submit(
                        self._run_feed, label, venue, state_path, health,
                        bars_changed=advanced,
                    )
                    for label, venue, state_path, health, advanced in plans
                }
                for label, future in futures.items():
                    result, seconds = future.result()
                    if label == "Gate":
                        gate_result, gate_seconds = result, seconds
                    else:
                        binance_result, binance_seconds = result, seconds
            # 顺序固定为 Gate 在前，摘要与权益差的口径不随线程完成先后变化。
            feed_results = [item for item in (gate_result, binance_result) if item is not None]

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
            # 计时必须包含通知发送：钉钉和SMTP都是网络调用，属于本轮真实开销。
            elapsed = (datetime.now(UTC) - cycle_started).total_seconds()
            # 两个交易所并行，各自耗时会重叠，因此总耗时不等于两者相加。
            # 死锁重试成功不会报错，不显式打出来就分不清"没发生"和"被吞了"。
            retries = take_deadlock_retry_count()
            retry_note = f"；死锁重试{retries}次" if retries else ""
            timing = (
                f"耗时{elapsed:.1f}秒"
                f"（并行：Gate {gate_seconds:.1f} / 币安 {binance_seconds:.1f}"
                f"，通知 {elapsed - max(gate_seconds, binance_seconds):.1f}）"
                f"{retry_note}"
            )
            signals = sum(item.summary.new_signal_count for item in feed_results)
            # 每秒一行日志会把有用的信息淹掉。只在有新收线K线、或本轮真的产生了
            # 交易信号时留痕；纯粹的在途复查轮次不写日志。
            if feed_results and (bars_changed or signals):
                details = "；".join(
                    f"{item.label}[{item.source}]权益"
                    f"{item.summary.paper_equity:.2f}U、信号{item.summary.new_signal_count}条"
                    for item in feed_results
                )
                self.reporter(f"量化服务第{completed_cycles}轮完成：{details}；{timing}")
            elif not feed_results:
                self.reporter(
                    f"量化服务第{completed_cycles}轮：两个行情源均失败；{timing}"
                )
            self._report_cycle_duration(elapsed)
            self.stop_event.wait(max(0.1, self.poll_seconds - elapsed))

        self.reporter(f"量化服务停止：成功调度 {completed_cycles} 轮")
        return completed_cycles
