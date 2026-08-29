"""逐笔交易诊断工具测试。"""

import pandas as pd

from gold_crypto_quant.backtest.diagnostics import _longest_losing_streak, _mean_or_zero


def test_longest_losing_streak() -> None:
    """连续亏损应在盈利交易出现时重新计数。"""
    pnl = pd.Series([-1.0, -2.0, 3.0, -4.0, -5.0, -6.0, 1.0])
    # 调用连亏统计方法，最后一段连续三笔亏损应为最大值。
    assert _longest_losing_streak(pnl) == 3


def test_mean_or_zero_handles_empty_series() -> None:
    """没有赢家或输家时，报告均值应返回0而不是NaN。"""
    # 调用安全均值方法，验证空序列可以直接进入JSON或命令行报告。
    assert _mean_or_zero(pd.Series(dtype=float)) == 0.0
