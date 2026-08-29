"""Gate历史K线写库前的数据转换和收盘过滤测试。"""

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from gold_crypto_quant.market_data.gate_history import _build_bar_rows, _split_contract


def test_contract_name_is_split_into_assets() -> None:
    # Gate使用下划线连接基础和计价资产，入库前需要拆成独立字段。
    assert _split_contract("BTC_USDT") == ("BTC", "USDT")


def test_open_candle_is_not_written_to_database() -> None:
    index = pd.DatetimeIndex(["2026-08-18T10:00:00Z", "2026-08-18T10:05:00Z"], name="open_time")
    frame = pd.DataFrame(
        {
            "open": [Decimal("100"), Decimal("101")],
            "high": [Decimal("102"), Decimal("103")],
            "low": [Decimal("99"), Decimal("100")],
            "close": [Decimal("101"), Decimal("102")],
            "volume": [Decimal("10"), Decimal("11")],
            "quote_volume": [Decimal("1005"), Decimal("1116")],
        },
        index=index,
    )

    # 10:07时第一根5分钟K线已收盘，第二根要到10:10才收盘，因此必须跳过第二根。
    rows, skipped_open = _build_bar_rows(
        frame,
        instrument_id=7,
        interval="5m",
        now=datetime(2026, 8, 18, 10, 7, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert skipped_open == 1
    assert rows[0]["instrument_id"] == 7
    assert rows[0]["close_price"] == Decimal("101")
    # MySQL DATETIME按项目约定保存无时区标记的UTC数值。
    assert rows[0]["open_time"].tzinfo is None


def test_naive_now_is_rejected() -> None:
    # 没有时区的当前时间可能把本地时间误当UTC，必须在写库前拒绝。
    empty_frame = pd.DataFrame()
    with pytest.raises(ValueError, match="timezone-aware"):
        _build_bar_rows(
            empty_frame,
            instrument_id=1,
            interval="5m",
            now=datetime(2026, 8, 18, 10, 0),
        )
