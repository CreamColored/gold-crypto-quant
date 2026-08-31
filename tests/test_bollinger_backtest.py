"""布林带策略准入决定的安全门禁测试。"""

from datetime import UTC, datetime

import pandas as pd

from gold_crypto_quant.backtest.bollinger_range import (
    BollingerBacktestResult,
    BollingerQualificationResult,
    build_bollinger_qualification_decision,
)
from gold_crypto_quant.strategy.bollinger_range import BollingerRangeParameters


def _fold(return_value: float, trades: int, profit_factor: float) -> BollingerBacktestResult:
    """构造一折固定回测摘要。"""
    return BollingerBacktestResult(
        symbol="ETH_USDT",
        start_time=pd.Timestamp("2026-01-01", tz="UTC"),
        end_time=pd.Timestamp("2026-01-02", tz="UTC"),
        bar_count=288,
        trade_count=trades,
        total_return=return_value,
        max_drawdown=0.02,
        win_rate=0.6,
        profit_factor=profit_factor,
        average_net_pnl=1.0,
        stop_count=2,
        middle_reduction_count=5,
        opposite_band_exit_count=3,
        daily_fuse_count=0,
        drawdown_fuse_triggered=False,
    )


def test_rejected_bollinger_result_uses_independent_strategy_identity() -> None:
    result = BollingerQualificationResult(
        symbol="ETH_USDT",
        interval="5m",
        parameters=BollingerRangeParameters(),
        folds=(_fold(-0.01, 2, 0.5), _fold(0.0, 0, 0.0), _fold(-0.01, 2, 0.5)),
        compounded_return=-0.0199,
        worst_drawdown=0.02,
        positive_folds=0,
        minimum_fold_trades=0,
        approved=False,
        reason="未通过",
    )

    # 调用统一准入转换，确认旧EMA审批不可能被新策略读取或复用。
    decision = build_bollinger_qualification_decision(
        result,
        evaluated_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert decision.strategy_name == "BOLLINGER_RANGE"
    assert decision.strategy_version == "3.0.0"
    assert decision.interval == "5m"
    assert decision.approved is False
    assert decision.details["parameters"]["fixed_stop_distance"] == 5.0
