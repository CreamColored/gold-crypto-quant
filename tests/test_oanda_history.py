"""OANDA黄金K线写库前转换测试。"""

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd

from gold_crypto_quant.market_data.oanda_history import _build_bar_rows, _split_instrument


def test_instrument_name_is_split_into_assets() -> None:
    assert _split_instrument("XAU_USD") == ("XAU", "USD")


def test_incomplete_oanda_candle_is_not_stored() -> None:
    frame = pd.DataFrame(
        {
            "open": [Decimal("2500"), Decimal("2501")],
            "high": [Decimal("2502"), Decimal("2503")],
            "low": [Decimal("2499"), Decimal("2500")],
            "close": [Decimal("2501"), Decimal("2502")],
            "volume": [Decimal("20"), Decimal("15")],
            "complete": [True, False],
        },
        index=pd.DatetimeIndex(
            ["2026-08-18T10:00:00Z", "2026-08-18T10:05:00Z"], name="open_time"
        ),
    )

    # 调用转换方法，接口明确标记未完成的第二根蜡烛必须被跳过。
    rows, skipped = _build_bar_rows(
        frame,
        instrument_id=9,
        interval="5m",
        now=datetime(2026, 8, 18, 10, 10, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert skipped == 1
    assert rows[0]["close_price"] == Decimal("2501")
    assert rows[0]["quote_volume"] is None
