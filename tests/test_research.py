"""时间隔离参数研究测试。"""

from dataclasses import replace

import pytest

from gold_crypto_quant.backtest.ema_vectorbt import EmaBacktestResult
from gold_crypto_quant.backtest.research import (
    _build_rolling_boundaries,
    _compound_returns,
    _research_score,
)


def make_result(*, trades: int, total_return: float, drawdown: float) -> EmaBacktestResult:
    """创建只包含评分所需字段的确定性回测结果。"""
    import pandas as pd

    return EmaBacktestResult(
        symbol="BTC_USDT",
        interval="30m",
        bar_count=1000,
        trade_count=trades,
        total_pnl=total_return * 10_000,
        total_return=total_return,
        max_drawdown=drawdown,
        win_rate=0.5,
        profit_factor=1.0,
        start_time=pd.Timestamp("2025-01-01", tz="UTC"),
        end_time=pd.Timestamp("2025-02-01", tz="UTC"),
        passed_drawdown_limit=True,
        daily_fuse_count=0,
        drawdown_fuse_triggered=False,
        drawdown_fuse_time=None,
    )


def test_research_score_rejects_too_few_trades() -> None:
    """低交易数候选即使零回撤也不能被选中。"""
    # 调用评分方法，7笔交易低于最低8笔要求。
    assert _research_score(make_result(trades=7, total_return=0.1, drawdown=0.0), 8) == float(
        "-inf"
    )


def test_research_score_penalizes_drawdown() -> None:
    """收益相同时应优先选择回撤更低的候选。"""
    low_drawdown = make_result(trades=10, total_return=0.05, drawdown=0.02)
    # replace调用仅改变回撤字段，其他候选条件保持完全一致。
    high_drawdown = replace(low_drawdown, max_drawdown=0.08)
    assert _research_score(low_drawdown, 8) > _research_score(high_drawdown, 8)


def test_rolling_boundaries_are_contiguous_and_non_overlapping() -> None:
    """三折边界应形成40/20、60/20、80/20扩展窗口。"""
    # 调用边界构造方法，1000根K线应产生三个连续的200根验证区间。
    boundaries = _build_rolling_boundaries(1000, fold_count=3, test_fraction=0.2)
    assert boundaries == [(400, 600), (600, 800), (800, 1000)]


def test_compound_returns_uses_continuous_capital() -> None:
    """滚动窗口收益必须复合，不能直接相加。"""
    # 调用复合收益方法，先涨10%再跌10%的结果应为-1%。
    assert _compound_returns([0.1, -0.1]) == pytest.approx(-0.01)
