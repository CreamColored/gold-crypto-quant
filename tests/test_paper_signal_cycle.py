"""模拟信号单轮主循环的行情与策略准入门禁测试。"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from gold_crypto_quant.market_data.oanda_history import OANDA_PRACTICE_VENUE
from gold_crypto_quant.runtime.paper_signal_cycle import run_paper_signal_cycle
from gold_crypto_quant.storage.strategy_signals import (
    QualificationContext,
    SavedStrategySignal,
)
from gold_crypto_quant.strategy.live_signals import NextBarSignalDecision, SignalAction


def _unused_bars(*_args: object, **_kwargs: object) -> pd.DataFrame:
    """信号计算在测试中被替换，因此只需返回占位DataFrame。"""
    return pd.DataFrame({"close": [100.0]})


def _entry_decision(*_args: object, **_kwargs: object) -> NextBarSignalDecision:
    """返回固定多头开仓决定以验证准入门禁。"""
    return NextBarSignalDecision(
        bar_time=datetime(2026, 8, 19, 12, 5, tzinfo=UTC),
        signal_price=Decimal("100"),
        actions=(SignalAction("LONG", "ENTRY"),),
        payload={},
    )


def _approved_details() -> dict[str, object]:
    """构造三折参数完全一致的批准审计明细。"""
    parameters = {
        "entry_mode": "cross",
        "pullback_lookback": 3,
        "atr_multiple": 1.5,
        "direction": "both",
        "min_trend_strength": 0.0,
        "min_adx": 0.0,
        "higher_timeframe_filter": False,
        "trend_slope_lookback": 0,
        "cooldown_bars": 0,
    }
    return {
        "stable_parameter_set": True,
        "folds": [{"parameters": dict(parameters)} for _fold in range(3)],
    }


def test_rejected_qualification_records_signal_but_creates_no_order() -> None:
    saved = SavedStrategySignal(
        signal_id=1,
        dedupe_key="a" * 64,
        side="LONG",
        signal_type="ENTRY",
        created=True,
    )

    # 调用主循环模拟健康行情出现开仓信号，但最新策略审批明确拒绝。
    summary = run_paper_signal_cycle(
        ("BTC_USDT",),
        ("5m",),
        health_refresher=lambda *_args: SimpleNamespace(status="HEALTHY", reason="ok"),
        bar_loader=_unused_bars,
        signal_evaluator=_entry_decision,
        signal_saver=lambda *_args: [saved],
        qualification_reader=lambda *_args: QualificationContext(
            instrument_id=1,
            decision="REJECTED",
            evaluation_hash="b" * 64,
            reason="样本外收益未通过",
        ),
    )

    stream = summary.streams[0]
    assert stream.status == "BLOCKED_QUALIFICATION"
    assert stream.new_signal_count == 1
    assert stream.order_count == 0
    assert summary.order_count == 0


def test_unhealthy_market_stops_before_loading_bars() -> None:
    calls: list[str] = []

    def should_not_load(*_args: object, **_kwargs: object) -> pd.DataFrame:
        calls.append("loaded")
        return pd.DataFrame()

    # 行情过期时调用主循环必须在K线读取和策略计算之前停止该行情流。
    summary = run_paper_signal_cycle(
        ("BTC_USDT",),
        ("5m",),
        health_refresher=lambda *_args: SimpleNamespace(status="STALE", reason="行情过期"),
        bar_loader=should_not_load,
    )

    assert summary.streams[0].status == "BLOCKED_MARKET_HEALTH"
    assert summary.order_count == 0
    assert calls == []


def test_approved_signal_is_still_fail_closed_before_position_sizing() -> None:
    # 即使准入批准，ATR止损和合约张数换算接入前也只能进入待计算状态。
    summary = run_paper_signal_cycle(
        ("BTC_USDT",),
        ("5m",),
        health_refresher=lambda *_args: SimpleNamespace(status="HEALTHY", reason="ok"),
        bar_loader=_unused_bars,
        signal_evaluator=_entry_decision,
        signal_saver=lambda *_args: [],
        qualification_reader=lambda *_args: QualificationContext(
            instrument_id=1,
            decision="APPROVED",
            evaluation_hash="b" * 64,
            reason="通过",
            details=_approved_details(),
        ),
    )

    assert summary.streams[0].status == "READY_FOR_POSITION_SIZING"
    assert summary.order_count == 0


def test_approved_entry_can_reserve_only_a_local_order() -> None:
    executions: list[str] = []

    def reserve_local(**kwargs: object) -> SimpleNamespace:
        executions.append(str(kwargs["symbol"]))
        return SimpleNamespace(
            order=SimpleNamespace(created=True),
            fill=SimpleNamespace(created=True),
        )

    # 批准策略调用注入的本地预留器，返回的订单明确只计入本地CREATED数量。
    summary = run_paper_signal_cycle(
        ("BTC_USDT",),
        ("5m",),
        health_refresher=lambda *_args: SimpleNamespace(status="HEALTHY", reason="ok"),
        bar_loader=_unused_bars,
        signal_evaluator=_entry_decision,
        signal_saver=lambda *_args: [],
        qualification_reader=lambda *_args: QualificationContext(
            instrument_id=1,
            decision="APPROVED",
            evaluation_hash="b" * 64,
            reason="通过",
            details=_approved_details(),
        ),
        entry_executor=reserve_local,
    )

    assert executions == ["BTC_USDT"]
    assert summary.streams[0].status == "PAPER_ENTRY_FILLED"
    assert summary.order_count == 1


def test_bar_gap_blocks_only_the_affected_stream() -> None:
    def load_with_one_failure(symbol: str, _interval: str, **_kwargs: object) -> pd.DataFrame:
        if symbol == "BTC_USDT":
            raise ValueError("market bar time gap found")
        return _unused_bars()

    # BTC数据缺口应失败关闭，但ETH仍继续完成本轮信号判断。
    summary = run_paper_signal_cycle(
        ("BTC_USDT", "ETH_USDT"),
        ("5m",),
        health_refresher=lambda *_args: SimpleNamespace(status="HEALTHY", reason="ok"),
        bar_loader=load_with_one_failure,
        signal_evaluator=lambda *_args, **_kwargs: NextBarSignalDecision(
            bar_time=datetime(2026, 8, 19, 12, 5, tzinfo=UTC),
            signal_price=Decimal("100"),
            actions=(),
            payload={},
        ),
        signal_saver=lambda *_args: [],
        qualification_reader=lambda *_args: QualificationContext(
            instrument_id=1,
            decision="REJECTED",
            evaluation_hash="b" * 64,
            reason="未通过",
        ),
    )

    assert summary.streams[0].status == "BLOCKED_DATA_ERROR"
    assert summary.streams[1].status == "NO_SIGNAL"
    assert summary.order_count == 0


def test_position_monitor_and_exit_run_before_rejected_opposite_entry() -> None:
    sequence: list[str] = []

    def monitor() -> None:
        sequence.append("monitor")

    def exit_position(**_kwargs: object) -> SimpleNamespace:
        sequence.append("exit")
        return SimpleNamespace(created=True)

    # 金叉同时包含平空和开多；即使开多审批被拒绝，已有空仓仍必须先退出。
    decision = NextBarSignalDecision(
        bar_time=datetime(2026, 8, 19, 12, 5, tzinfo=UTC),
        signal_price=Decimal("100"),
        actions=(SignalAction("SHORT", "EXIT"), SignalAction("LONG", "ENTRY")),
        payload={},
    )
    summary = run_paper_signal_cycle(
        ("BTC_USDT",),
        ("5m",),
        health_refresher=lambda *_args: SimpleNamespace(status="HEALTHY", reason="ok"),
        bar_loader=_unused_bars,
        signal_evaluator=lambda *_args, **_kwargs: decision,
        signal_saver=lambda *_args: [],
        qualification_reader=lambda *_args: QualificationContext(
            instrument_id=1,
            decision="REJECTED",
            evaluation_hash="b" * 64,
            reason="未通过",
        ),
        exit_executor=exit_position,
        position_monitor=monitor,
    )

    assert sequence == ["monitor", "exit"]
    assert summary.streams[0].status == "BLOCKED_QUALIFICATION"
    assert summary.streams[0].order_count == 1


def test_oanda_signal_cycle_propagates_venue_and_keeps_orders_closed() -> None:
    venues: list[str] = []

    def health(_symbol: str, _interval: str, *, venue: str) -> SimpleNamespace:
        venues.append(venue)
        return SimpleNamespace(status="HEALTHY", reason="ok")

    def qualification(
        _symbol: str, _interval: str, *, venue: str
    ) -> QualificationContext:
        venues.append(venue)
        return QualificationContext(
            instrument_id=9,
            decision="REJECTED",
            evaluation_hash="c" * 64,
            reason="黄金样本外收益未通过",
        )

    def bars(
        _symbol: str, _interval: str, *, limit: int, venue: str
    ) -> pd.DataFrame:
        assert limit == 500
        venues.append(venue)
        return _unused_bars()

    def save(
        _symbol: str,
        _interval: str,
        _decision: NextBarSignalDecision,
        *,
        venue: str,
    ) -> list[SavedStrategySignal]:
        venues.append(venue)
        return []

    # 调用黄金观察周期，确认所有数据库读取都固定到OANDA且拒绝准入不会创建订单。
    summary = run_paper_signal_cycle(
        ("XAU_USD",),
        ("5m",),
        venue=OANDA_PRACTICE_VENUE,
        health_refresher=health,
        qualification_reader=qualification,
        bar_loader=bars,
        signal_evaluator=_entry_decision,
        signal_saver=save,
    )

    assert venues == [OANDA_PRACTICE_VENUE] * 4
    assert summary.streams[0].status == "BLOCKED_QUALIFICATION"
    assert summary.order_count == 0
