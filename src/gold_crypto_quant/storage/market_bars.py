"""从 MySQL 读取标准化K线供策略和回测使用。"""

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.market_data.oanda_history import OANDA_PRACTICE_VENUE
from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import Instrument, MarketBar

EXPECTED_INTERVALS = {
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
}


def _is_us_holiday_near(timestamp: pd.Timestamp) -> bool:
    """判断时间所在日或次日是否为美国市场常见节假日。"""
    calendar = USFederalHolidayCalendar()
    day = timestamp.normalize().tz_localize(None)
    # 调用pandas节假日日历，同时覆盖节日前一晚的金属提前休市。
    holidays = calendar.holidays(start=day, end=day + pd.Timedelta(days=1))
    return len(holidays) > 0


def _is_oanda_session_gap(
    previous_time: pd.Timestamp,
    current_time: pd.Timestamp,
    expected_delta: pd.Timedelta,
) -> bool:
    """识别黄金周末、节假日和纽约收盘附近的预期休市缺口。"""
    actual_delta = current_time - previous_time
    if actual_delta == expected_delta:
        return True
    if actual_delta <= pd.Timedelta(0) or actual_delta % expected_delta != pd.Timedelta(0):
        return False
    # 周末或整日节假日会形成接近一天或更长的缺口；保留缺口，不填造假K线。
    if actual_delta >= pd.Timedelta(hours=20):
        return True
    # 美国节假日黄金可能提前数小时休市；只允许傍晚开始且不超过8小时的窗口。
    if (
        actual_delta <= pd.Timedelta(hours=8)
        and previous_time.hour >= 17
        and _is_us_holiday_near(previous_time)
    ):
        return True
    # OANDA金属每日维护通常发生在纽约收盘附近；DST会让UTC时间在20至23点间移动。
    return bool(
        actual_delta <= pd.Timedelta(hours=2) + expected_delta
        and previous_time.hour in {20, 21, 22, 23}
    )


def _validate_market_bars(
    frame: pd.DataFrame,
    symbol: str,
    interval: str,
    *,
    venue: str = GATE_TESTNET_VENUE,
) -> None:
    """检查时间连续性、价格正数约束和OHLC内部关系。"""
    if interval not in EXPECTED_INTERVALS:
        raise ValueError(f"unsupported market bar interval: {interval}")
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError(f"invalid market bar index for {symbol} {interval}")

    # 相邻开盘时间必须严格等于周期长度，缺K线时停止回测，避免生成失真的EMA信号。
    actual_deltas = frame.index.to_series().diff().dropna()
    expected_delta = EXPECTED_INTERVALS[interval]
    if venue == OANDA_PRACTICE_VENUE:
        timestamps = list(frame.index)
        valid_deltas = all(
            _is_oanda_session_gap(previous, current, expected_delta)
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        )
    else:
        valid_deltas = actual_deltas.eq(expected_delta).all()
    if not valid_deltas:
        raise ValueError(f"market bar time gap found for {venue} {symbol} {interval}")

    price_columns = ["open", "high", "low", "close"]
    if frame[price_columns].isna().any().any() or frame[price_columns].le(0).any().any():
        raise ValueError(f"non-positive or missing price found for {symbol} {interval}")
    # 每根K线的最高价不能低于开收盘价，最低价不能高于开收盘价。
    body_high = frame[["open", "close"]].max(axis=1)
    body_low = frame[["open", "close"]].min(axis=1)
    if frame["high"].lt(body_high).any() or frame["low"].gt(body_low).any():
        raise ValueError(f"invalid OHLC relationship for {symbol} {interval}")


def load_market_bars(
    symbol: str,
    interval: str,
    *,
    engine: Engine | None = None,
    venue: str = GATE_TESTNET_VENUE,
    limit: int | None = None,
) -> pd.DataFrame:
    """按品种和周期读取已收盘K线，并转换成UTC时间升序的DataFrame。"""
    if limit is not None and limit < 2:
        raise ValueError("market bar limit must be at least two")
    engine = engine or build_engine()
    statement = (
        select(
            MarketBar.open_time,
            MarketBar.open_price,
            MarketBar.high_price,
            MarketBar.low_price,
            MarketBar.close_price,
            MarketBar.volume,
            MarketBar.quote_volume,
        )
        .join(Instrument, Instrument.id == MarketBar.instrument_id)
        .where(
            Instrument.venue == venue,
            Instrument.symbol == symbol,
            MarketBar.interval_code == interval,
            MarketBar.is_closed.is_(True),
        )
    )
    if limit is None:
        statement = statement.order_by(MarketBar.open_time)
    else:
        # 长期运行只调用最近窗口，先按时间倒序让LIMIT取得最新记录。
        statement = statement.order_by(MarketBar.open_time.desc()).limit(limit)

    # Session只负责执行只读查询；离开with后连接会自动归还连接池。
    with Session(engine) as session:
        rows = session.execute(statement).all()
    if limit is not None:
        # 数据库倒序取最新窗口后调用reverse恢复策略要求的时间升序。
        rows.reverse()
    if not rows:
        raise ValueError(f"no closed market bars found for {venue} {symbol} {interval}")

    # 策略计算使用float；数据库仍保留DECIMAL原始精度，转换只发生在内存副本中。
    frame = pd.DataFrame(
        rows,
        columns=["open_time", "open", "high", "low", "close", "volume", "quote_volume"],
    )
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame = frame.set_index("open_time")
    numeric_columns = ["open", "high", "low", "close", "volume", "quote_volume"]
    frame[numeric_columns] = frame[numeric_columns].astype(float)

    # 在把K线交给策略前统一执行质量检查，避免缺失或错误行情污染回测结果。
    _validate_market_bars(frame, symbol, interval, venue=venue)
    return frame
