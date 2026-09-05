"""币安U本位永续实盘公共行情客户端；不包含密钥、账户或下单方法。"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
import pandas as pd

SUPPORTED_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "1h"})
BINANCE_FUTURES_HOST = "fapi.binance.com"
# PERPETUAL：常规加密货币永续合约；TRADIFI_PERPETUAL：2026年上线的受监管黄金/白银永续合约。
TRADABLE_CONTRACT_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})


class BinanceApiError(RuntimeError):
    """币安公共接口返回错误状态或无法识别的数据结构。"""


class BinancePublicClient:
    """只读取币安U本位永续公开行情，构造函数不提供任何认证参数。"""

    def __init__(
        self,
        *,
        base_url: str = "https://fapi.binance.com",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if urlsplit(base_url).hostname != BINANCE_FUTURES_HOST:
            raise ValueError(f"Binance public client only allows host: {BINANCE_FUTURES_HOST}")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        # 合约目录在进程运行期间基本不变；每个品种首次校验后缓存，避免每分钟
        # 为 BTC、ETH 重复下载整份 exchangeInfo。
        self._symbol_cache: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        """关闭HTTP连接池。"""
        self._http.close()

    def __enter__(self) -> "BinancePublicClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_exchange_symbol(self, symbol: str) -> dict[str, Any]:
        """确认合约存在、可交易且属于永续合约。"""
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached
        data = self._request_json("/fapi/v1/exchangeInfo")
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            raise BinanceApiError("Binance exchangeInfo response is invalid")
        match = next((item for item in data["symbols"] if item.get("symbol") == symbol), None)
        if match is None:
            raise BinanceApiError(f"Binance futures symbol not found: {symbol}")
        is_tradable = match.get("contractType") in TRADABLE_CONTRACT_TYPES
        if not is_tradable or match.get("status") != "TRADING":
            raise BinanceApiError(f"Binance futures symbol is not tradable perpetual: {symbol}")
        self._symbol_cache[symbol] = match
        return match

    def get_candlesticks(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 500,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> pd.DataFrame:
        """读取公开成交K线并标准化为内部OHLCV格式。"""
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"unsupported Binance interval: {interval}")
        if not 1 <= limit <= 1500:
            raise ValueError("Binance candlestick limit must be in [1, 1500]")
        params: dict[str, object] = {"symbol": symbol, "interval": interval, "limit": limit}
        for name, value in (("startTime", start_time), ("endTime", end_time)):
            if value is None:
                continue
            if value.tzinfo is None:
                raise ValueError("candlestick time range must be timezone-aware")
            params[name] = int(value.timestamp() * 1000)
        data = self._request_json("/fapi/v1/klines", params=params)
        if not isinstance(data, list):
            raise BinanceApiError("Binance candlestick response must be a list")
        rows: list[dict[str, object]] = []
        for candle in data:
            if not isinstance(candle, list) or len(candle) < 11:
                raise BinanceApiError("Binance candlestick item is invalid")
            rows.append(
                {
                    "open_time": int(candle[0]),
                    "open": Decimal(str(candle[1])),
                    "high": Decimal(str(candle[2])),
                    "low": Decimal(str(candle[3])),
                    "close": Decimal(str(candle[4])),
                    "volume": Decimal(str(candle[5])),
                    "quote_volume": Decimal(str(candle[7])),
                    "trade_count": int(candle[8]),
                }
            )
        columns = ["open", "high", "low", "close", "volume", "quote_volume", "trade_count"]
        if not rows:
            return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC"))
        frame = pd.DataFrame.from_records(rows)
        frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
        frame = frame.set_index("open_time").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        return frame[columns]

    def _request_json(self, path: str, *, params: dict[str, object] | None = None) -> Any:
        """调用无需签名的GET接口并统一转换错误。"""
        response = self._http.get(path, params=params or {})
        try:
            data = response.json()
        except ValueError as exc:
            raise BinanceApiError(
                f"Binance returned non-JSON response: HTTP {response.status_code}"
            ) from exc
        if response.is_error:
            code = data.get("code", "UNKNOWN") if isinstance(data, dict) else "UNKNOWN"
            message = data.get("msg", str(data)) if isinstance(data, dict) else str(data)
            raise BinanceApiError(f"Binance API error {response.status_code} [{code}]: {message}")
        return data
