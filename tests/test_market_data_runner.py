"""行情长期运行器、断线退避和单实例保护测试。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gold_crypto_quant.config import Settings
from gold_crypto_quant.market_data.gate_history import ImportResult
from gold_crypto_quant.runtime.market_data_runner import (
    MarketDataRunner,
    RunnerConfig,
    SingleInstanceLock,
    retry_delay_seconds,
    safe_error_summary,
)


class FakeClient:
    """只验证上下文管理和关闭路径的测试客户端。"""

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class ImmediateEvent:
    """记录等待时间但不真实休眠，便于测试重试后恢复。"""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return self.stopped


def _settings() -> Settings:
    """构造不读取真实密钥的测试配置。"""
    return Settings(
        _env_file=None,
        gate_testnet_api_key="test-key",
        gate_testnet_api_secret="test-secret",
    )


def _result(stored: int = 7) -> ImportResult:
    """生成一条固定的K线导入统计。"""
    return ImportResult(
        contract="BTC_USDT",
        interval="5m",
        fetched=8,
        stored=stored,
        skipped_open=1,
        pages=1,
        history_limited=False,
    )


def test_retry_delay_uses_capped_exponential_backoff() -> None:
    # 连续失败等待5、10、20秒，之后始终封顶30秒。
    assert [
        retry_delay_seconds(failures, initial_seconds=5, maximum_seconds=30)
        for failures in range(1, 6)
    ] == [5, 10, 20, 30, 30]


def test_error_summary_redacts_credentials() -> None:
    error = RuntimeError(
        "mysql+pymysql://quant:clear-password@127.0.0.1/db password=oops token:abc"
    )

    # 保存到数据库或日志前必须移除数据库密码、普通密码和令牌。
    summary = safe_error_summary(error)

    assert "clear-password" not in summary
    assert "oops" not in summary
    assert "abc" not in summary
    assert "***" in summary


def test_runner_refreshes_health_after_successful_import() -> None:
    states: list[dict[str, Any]] = []
    health_calls: list[tuple[str, str]] = []
    reports: list[str] = []
    after_cycle_calls: list[str] = []
    event = ImmediateEvent()

    def import_once(_client: FakeClient, **kwargs: object) -> list[ImportResult]:
        assert kwargs["contracts"] == ("BTC_USDT",)
        assert kwargs["intervals"] == ("5m",)
        return [_result()]

    runner = MarketDataRunner(
        _settings(),
        RunnerConfig(contracts=("BTC_USDT",), intervals=("5m",), max_cycles=1),
        stop_event=event,  # type: ignore[arg-type]
        client_factory=FakeClient,  # type: ignore[arg-type]
        importer=import_once,  # type: ignore[arg-type]
        health_refresher=lambda symbol, interval: health_calls.append((symbol, interval)),
        state_saver=lambda **kwargs: states.append(kwargs),
        after_cycle=lambda: after_cycle_calls.append("called"),
        clock=lambda: datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
        reporter=reports.append,
    )

    # 调用运行器一轮验证路径，不等待真实的60秒轮询间隔。
    result = runner.run()

    assert result.successful_cycles == 1
    assert health_calls == [("BTC_USDT", "5m")]
    assert after_cycle_calls == ["called"]
    assert [state["status"] for state in states] == ["STARTING", "RUNNING", "STOPPED"]
    assert any("刷新7条" in report for report in reports)
    assert event.waits == []


def test_runner_can_persist_an_isolated_service_name() -> None:
    states: list[dict[str, Any]] = []
    runner = MarketDataRunner(
        _settings(),
        RunnerConfig(contracts=("XAU_USD",), intervals=("5m",), max_cycles=1),
        stop_event=ImmediateEvent(),  # type: ignore[arg-type]
        client_factory=FakeClient,  # type: ignore[arg-type]
        importer=lambda _client, **_kwargs: [_result()],
        health_refresher=lambda _symbol, _interval: None,
        state_saver=lambda **kwargs: states.append(kwargs),
        service_name="OANDA_PRACTICE_MARKET_DATA",
        clock=lambda: datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
    )

    # 调用同一运行器内核，确认OANDA心跳不会覆盖Gate服务状态。
    runner.run()

    assert {state["service_name"] for state in states} == {
        "OANDA_PRACTICE_MARKET_DATA"
    }


def test_runner_reconnects_after_failure_and_resets_failure_count() -> None:
    states: list[dict[str, Any]] = []
    event = ImmediateEvent()
    attempts = 0

    def fail_then_succeed(_client: FakeClient, **_kwargs: object) -> list[ImportResult]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary disconnect")
        return [_result(3)]

    runner = MarketDataRunner(
        _settings(),
        RunnerConfig(
            contracts=("BTC_USDT",),
            intervals=("5m",),
            retry_initial_seconds=2,
            retry_max_seconds=20,
            max_cycles=1,
        ),
        stop_event=event,  # type: ignore[arg-type]
        client_factory=FakeClient,  # type: ignore[arg-type]
        importer=fail_then_succeed,  # type: ignore[arg-type]
        health_refresher=lambda _symbol, _interval: None,
        state_saver=lambda **kwargs: states.append(kwargs),
        clock=lambda: datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
    )

    # 第一次连接失败后立即模拟等待完成，第二次应创建新客户端并恢复成功。
    result = runner.run()

    assert attempts == 2
    assert result.successful_cycles == 1
    assert event.waits == [2]
    assert [state["status"] for state in states] == [
        "STARTING",
        "RETRYING",
        "RUNNING",
        "STOPPED",
    ]
    assert states[1]["consecutive_failures"] == 1
    assert states[2]["consecutive_failures"] == 0


def test_single_instance_lock_rejects_second_process_slot(tmp_path: Path) -> None:
    lock_path = tmp_path / "runner.lock"

    # 第一个锁持有期间再次获取同一路径必须立即失败，防止重复采集进程。
    with SingleInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="already active"):
            with SingleInstanceLock(lock_path):
                pytest.fail("second lock should not be acquired")
