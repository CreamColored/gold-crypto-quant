"""指标缓存的失效测试。

缓存错了不会报错，只会让策略拿着旧轨道做决策——这类 bug 没有任何外部症状，只能
靠测试把失效条件钉死。指纹必须对"追加了新K线""窗口滑动了""数值被改写"三种变化
全部敏感。
"""

import pandas as pd

from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    _cached,
    _frame_fingerprint,
    reset_context_cache,
)


def _frame(periods: int = 30, last_close: float = 100.0, start: str = "2026-01-01") -> pd.DataFrame:
    index = pd.date_range(start=start, periods=periods, freq="1min", tz="UTC")
    closes = [100.0] * periods
    closes[-1] = last_close
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": closes,
         "volume": 10.0, "quote_volume": 1000.0},
        index=index,
    )


def test_identical_frames_share_a_fingerprint() -> None:
    assert _frame_fingerprint(_frame()) == _frame_fingerprint(_frame())


def test_appending_a_bar_changes_the_fingerprint() -> None:
    """新K线收线后必须重算，否则策略会拿着上一分钟的轨道开仓。"""
    assert _frame_fingerprint(_frame(30)) != _frame_fingerprint(_frame(31))


def test_sliding_the_window_changes_the_fingerprint() -> None:
    """回测里窗口长度不变但起点在动，只看长度和末根会误命中。"""
    a = _frame(30, start="2026-01-01 00:00")
    b = _frame(30, start="2026-01-01 00:05")
    assert _frame_fingerprint(a) != _frame_fingerprint(b)


def test_revised_last_close_changes_the_fingerprint() -> None:
    """同样长度、同样时间跨度，但末根数值被改写过。"""
    assert _frame_fingerprint(_frame(30, 100.0)) != _frame_fingerprint(_frame(30, 105.0))


def test_cache_returns_the_stored_value_and_skips_rebuilding() -> None:
    reset_context_cache()
    calls = []

    def build():
        calls.append(1)
        return pd.DataFrame({"x": [1]})

    key = ("probe", _frame_fingerprint(_frame()))
    first = _cached(key, build)
    second = _cached(key, build)
    assert first is second
    assert len(calls) == 1


def test_different_keys_do_not_collide() -> None:
    reset_context_cache()
    a = _cached(("a",), lambda: pd.DataFrame({"x": [1]}))
    b = _cached(("b",), lambda: pd.DataFrame({"x": [2]}))
    assert a is not b and int(b["x"].iloc[0]) == 2


def test_reset_forces_a_rebuild() -> None:
    calls = []

    def build():
        calls.append(1)
        return pd.DataFrame({"x": [1]})

    reset_context_cache()
    _cached(("probe",), build)
    reset_context_cache()
    _cached(("probe",), build)
    assert len(calls) == 2
