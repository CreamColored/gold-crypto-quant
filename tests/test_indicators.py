"""ATR之外的趋势指标测试。"""

import numpy as np
import pandas as pd

from gold_crypto_quant.risk.indicators import average_directional_index


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
