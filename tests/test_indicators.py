"""ATR之外的趋势指标测试。"""

import numpy as np
import pandas as pd

from gold_crypto_quant.risk.indicators import average_directional_index, choppiness_index


def test_adx_is_high_for_persistent_one_way_trend() -> None:
    """持续单向上涨行情的ADX应在预热后接近强趋势区间。"""
    index = pd.date_range("2025-01-01", periods=100, freq="1h", tz="UTC")
    close = np.linspace(100.0, 200.0, len(index))
    bars = pd.DataFrame(
        {"high": close + 1.0, "low": close - 1.0, "close": close},
        index=index,
    )
    # 调用ADX方法，稳定单向趋势末端应明显高于常用25阈值。
    adx = average_directional_index(bars, period=14)
    assert adx.iloc[-1] > 50


def test_choppiness_is_higher_for_back_and_forth_prices_than_for_trend() -> None:
    index = pd.date_range("2026-01-01", periods=80, freq="15min", tz="UTC")
    trend_close = pd.Series(range(100, 180), index=index, dtype=float)
    chop_close = pd.Series([100.0, 104.0, 100.0, 104.0] * 20, index=index)

    def bars(close: pd.Series) -> pd.DataFrame:
        return pd.DataFrame({"high": close + 1, "low": close - 1, "close": close}, index=index)

    assert choppiness_index(bars(chop_close)).iloc[-1] > choppiness_index(bars(trend_close)).iloc[-1]
