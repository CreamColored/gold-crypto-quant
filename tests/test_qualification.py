"""策略模拟交易准入规则测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd

from gold_crypto_quant.risk.qualification import evaluate_rolling_research


def make_rolling_result(
    *,
    compounded_return: float,
    worst_drawdown: float,
    fold_returns: tuple[float, float, float],
    fold_trades: tuple[int, int, int],
) -> SimpleNamespace:
    """构造只包含准入评估所需字段的滚动结果。"""
    folds = []
    for number, (fold_return, trades) in enumerate(
        zip(fold_returns, fold_trades, strict=True), start=1
    ):
        selected_result = SimpleNamespace(
            total_return=fold_return,
            max_drawdown=worst_drawdown,
            trade_count=trades,
        )
        selection = SimpleNamespace(
            selected_test_result=selected_result,
            entry_mode="cross",
            pullback_lookback=3,
            atr_multiple=1.5,
            direction="both",
            min_trend_strength=0.0,
            min_adx=0.0,
            use_higher_timeframe_filter=False,
            trend_slope_lookback=0,
            cooldown_bars=0,
        )
        folds.append(
            SimpleNamespace(
                fold=number,
                test_start=pd.Timestamp(f"2025-0{number}-01", tz="UTC"),
                test_end=pd.Timestamp(f"2025-0{number + 1}-01", tz="UTC"),
                research=selection,
            )
        )
    return SimpleNamespace(
        symbol="BTC_USDT",
        interval="30m",
        folds=tuple(folds),
        baseline_compounded_return=-0.05,
        selected_compounded_return=compounded_return,
        selected_worst_drawdown=worst_drawdown,
        selected_positive_folds=sum(value > 0 for value in fold_returns),
        stable_parameter_set=True,
    )


def test_profitable_robust_research_is_approved() -> None:
    """四项门槛全部满足时才批准进入模拟交易。"""
    rolling = make_rolling_result(
        compounded_return=0.06,
        worst_drawdown=0.04,
        fold_returns=(0.02, 0.01, 0.03),
        fold_trades=(10, 12, 9),
    )
    # 调用准入评估，验证正收益、低回撤、盈利窗口和交易数联合通过。
    decision = evaluate_rolling_research(rolling)
    assert decision.approved
    assert decision.reason == "通过全部样本外准入门槛"


def test_losing_low_sample_research_is_rejected() -> None:
    """负收益、无盈利窗口和低交易数应同时出现在拒绝原因中。"""
    rolling = make_rolling_result(
        compounded_return=-0.05,
        worst_drawdown=0.05,
        fold_returns=(-0.01, -0.02, -0.02),
        fold_trades=(4, 5, 6),
    )
    # 调用准入评估，任何失败条件都不能被其他较好指标抵消。
    decision = evaluate_rolling_research(rolling)
    assert not decision.approved
    assert "样本外复合收益" in decision.reason
    assert "盈利窗口" in decision.reason
    assert "单折最少交易" in decision.reason


def test_identical_metrics_produce_same_evaluation_hash() -> None:
    """评估时间不同不应造成相同研究结果重复入库。"""
    rolling = make_rolling_result(
        compounded_return=0.06,
        worst_drawdown=0.04,
        fold_returns=(0.02, 0.01, 0.03),
        fold_trades=(10, 12, 9),
    )
    # 两次调用使用不同时间，评估内容不变时哈希必须保持一致。
    first = evaluate_rolling_research(
        rolling,
        evaluated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    second = evaluate_rolling_research(
        rolling,
        evaluated_at=datetime(2025, 1, 2, tzinfo=UTC),
    )
    assert first.evaluation_hash == second.evaluation_hash
