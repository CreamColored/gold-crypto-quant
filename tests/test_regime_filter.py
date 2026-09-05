"""V5.9 震荡识别的四条判据。

数据量级按真实 ETH 取：震荡段带宽约 1%、非震荡段约 2%。
V5.8 的合成用例是 [97, 103] 逐根交替（带宽 12%），那个量级在真实市场里
不是箱体，所以这里不沿用。
"""

import numpy as np
import pandas as pd
import pytest

from gold_crypto_quant.strategy import bollinger_range
from gold_crypto_quant.strategy.bollinger_range import (
    build_rotation_box_context,
    parameters_for_same_timeframe,
)

BASE = 2450.0


def _frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    series = pd.Series(closes, index=index)
    return pd.DataFrame(
        {
            "open": series,
            "high": series + BASE * 0.0005,
            "low": series - BASE * 0.0005,
            "close": series,
            "volume": pd.Series(1000.0, index=index),
        }
    )


def _range_market(n: int = 60) -> pd.DataFrame:
    """真实量级的震荡：围绕 2450 上下 0.25%，反复穿越中轨。"""
    steps = np.arange(n)
    return _frame(list(BASE + BASE * 0.0025 * np.sin(steps * np.pi / 3)))


def _trend_market(n: int = 60) -> pd.DataFrame:
    """单边上涨：贴着上轨走，中轨很少被穿，带宽持续开口。

    必须是**加速**上涨。完全线性的涨幅其滚动标准差是恒定的，带宽不会开口——
    那不是真实单边行情的样子，用它测"开口"会得到反向结论。
    """
    steps = np.arange(n)
    return _frame(list(BASE * (1 + 0.0004 * steps + 0.00002 * steps**2)))


def test_real_scale_range_qualifies() -> None:
    context = build_rotation_box_context(_range_market(), parameters_for_same_timeframe("15m"))
    latest = context.iloc[-1]
    assert bool(latest["box_candidate"]) is True
    assert float(latest["relative_width"]) <= 1.2
    assert float(latest["middle_crossings"]) >= 3


def test_trend_is_rejected() -> None:
    """单边行情必须被否掉——V5.8 在这里精确率只有 58%，正是亏损来源。"""
    context = build_rotation_box_context(_trend_market(), parameters_for_same_timeframe("15m"))
    assert bool(context.iloc[-1]["box_candidate"]) is False


def test_middle_crossings_separate_range_from_trend() -> None:
    """中轨穿越次数是 V5.8 完全没有的维度，也是人工标注里区分度第二强的特征。"""
    ranging = build_rotation_box_context(_range_market(), parameters_for_same_timeframe("15m"))
    trending = build_rotation_box_context(_trend_market(), parameters_for_same_timeframe("15m"))
    assert float(ranging.iloc[-1]["middle_crossings"]) > float(
        trending.iloc[-1]["middle_crossings"]
    )


def test_expanding_bands_are_rejected() -> None:
    """带宽开口意味着单边行情正在展开；标注数据里非震荡段是 +71%～+130%。"""
    context = build_rotation_box_context(_trend_market(), parameters_for_same_timeframe("15m"))
    assert float(context.iloc[-1]["width_growth"]) > 0.0


def test_filter_can_be_disabled_for_ab_comparison(monkeypatch) -> None:
    """关掉判据要能复现 V5.8 的行为，否则没法做 A/B 对照。"""
    monkeypatch.setattr(bollinger_range, "REGIME_FILTER_ENABLED", False)
    context = build_rotation_box_context(_trend_market(), parameters_for_same_timeframe("15m"))
    assert "relative_width" not in context.columns


@pytest.mark.parametrize("interval,ceiling", [("5m", 0.9), ("15m", 1.2), ("30m", 1.8)])
def test_width_ceiling_scales_with_interval(interval: str, ceiling: float) -> None:
    """周期越大带宽天然越宽，用同一个上限会让大周期永远不合格。"""
    assert parameters_for_same_timeframe(interval).maximum_relative_width == ceiling
