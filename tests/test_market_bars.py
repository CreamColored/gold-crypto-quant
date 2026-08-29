"""MySQL标准化K线质量检查测试。"""

import pandas as pd
import pytest

from gold_crypto_quant.market_data.oanda_history import OANDA_PRACTICE_VENUE
from gold_crypto_quant.storage.market_bars import _validate_market_bars


def make_bars() -> pd.DataFrame:
    """创建三根合法且连续的5分钟K线。"""
    index = pd.date_range("2025-01-01", periods=3, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
        },
        index=index,
    )


def test_validate_market_bars_accepts_valid_data() -> None:
    """合法K线不应触发异常。"""
    # 调用质量检查方法，正常返回即表示通过。
    _validate_market_bars(make_bars(), "BTC_USDT", "5m")


def test_validate_market_bars_rejects_time_gap() -> None:
    """缺失中间K线时必须停止后续回测。"""
    bars = make_bars().drop(make_bars().index[1])
    with pytest.raises(ValueError, match="time gap"):
        # 调用检查方法确认10分钟断档能被识别。
        _validate_market_bars(bars, "BTC_USDT", "5m")


def test_validate_market_bars_rejects_invalid_high() -> None:
    """最高价低于收盘价属于非法OHLC关系。"""
    bars = make_bars()
    bars.loc[bars.index[0], "high"] = 100.5
    with pytest.raises(ValueError, match="invalid OHLC"):
        # 调用检查方法确认错误价格不会进入策略。
        _validate_market_bars(bars, "ETH_USDT", "5m")


def test_oanda_weekend_session_gap_is_allowed() -> None:
    bars = make_bars().iloc[:2].copy()
    bars.index = pd.DatetimeIndex(
        ["2026-08-21T21:55:00Z", "2026-08-23T22:00:00Z"]
    )

    # 调用OANDA专属检查，周末休市不应被误判为行情丢失。
    _validate_market_bars(
        bars,
        "XAU_USD",
        "5m",
        venue=OANDA_PRACTICE_VENUE,
    )


def test_oanda_intraday_missing_bar_is_rejected() -> None:
    bars = make_bars().iloc[:2].copy()
    bars.index = pd.DatetimeIndex(
        ["2026-08-18T10:00:00Z", "2026-08-18T10:10:00Z"]
    )

    with pytest.raises(ValueError, match="market bar time gap"):
        # 调用OANDA专属检查，普通交易时段缺失一根5分钟K线仍必须阻断研究。
        _validate_market_bars(
            bars,
            "XAU_USD",
            "5m",
            venue=OANDA_PRACTICE_VENUE,
        )


def test_oanda_us_holiday_early_close_is_allowed() -> None:
    bars = make_bars().iloc[:2].copy()
    bars.index = pd.DatetimeIndex(
        ["2026-05-25T18:25:00Z", "2026-05-25T22:00:00Z"]
    )

    # 美国阵亡将士纪念日的黄金提前休市属于预期市场时段，不是行情断线。
    _validate_market_bars(
        bars,
        "XAU_USD",
        "5m",
        venue=OANDA_PRACTICE_VENUE,
    )
