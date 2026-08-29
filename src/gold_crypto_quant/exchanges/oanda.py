"""OANDA v20 Practice只读行情客户端。

客户端被硬锁到官方Practice域名，并且只提供历史K线查询，不实现任何订单方法。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
import pandas as pd

from gold_crypto_quant.config import Settings, get_settings

OANDA_PRACTICE_HOST = "api-fxpractice.oanda.com"
SUPPORTED_INTERVALS = frozenset({"5m", "15m", "30m", "1h"})
OANDA_GRANULARITY = {"5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1"}


class OandaApiError(RuntimeError):
    """OANDA返回非成功状态或不符合预期的数据结构。"""


@dataclass(frozen=True, slots=True)
class OandaPracticeAccount:
    """OANDA Practice账户风控需要的只读资金摘要。"""

    account_id: str
    currency: str
    balance: Decimal
    nav: Decimal
    margin_available: Decimal
    margin_used: Decimal
    unrealized_pl: Decimal
    hedging_enabled: bool


@dataclass(frozen=True, slots=True)
class OandaInstrumentRules:
    """OANDA账户下单单位、价格精度和保证金规则的只读摘要。"""

    name: str
    display_precision: int
    trade_units_precision: int
    minimum_trade_size: Decimal
    maximum_order_units: Decimal
    maximum_position_size: Decimal
    margin_rate: Decimal


@dataclass(frozen=True, slots=True)
class OandaPrice:
    """OANDA Practice当前可交易买卖价。"""

    instrument: str
    bid: Decimal
    ask: Decimal
    tradeable: bool


def resolve_oanda_account_id(
    client: "OandaPracticeClient", configured_account_id: str | None
) -> tuple[str, bool]:
    """校验配置账户；仅有一个授权账户时允许自动选择。"""
    # 调用只读账户列表，确认Token和账户编号属于同一个Practice环境。
    account_ids = client.get_authorized_account_ids()
    if configured_account_id:
        if configured_account_id not in account_ids:
            raise ValueError("configured OANDA account is not authorized by this token")
        return configured_account_id, False
    if len(account_ids) != 1:
        raise ValueError(
            "OANDA_PRACTICE_ACCOUNT_ID is required when token authorizes multiple accounts"
        )
    return account_ids[0], True


class OandaPracticeClient:
    """封装OANDA v20 Practice的只读蜡烛行情请求。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed_url = urlsplit(base_url)
        # 安全锁：拒绝实盘域名、非HTTPS协议和带额外路径的伪装地址。
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != OANDA_PRACTICE_HOST
            or parsed_url.path.rstrip("/")
        ):
            raise ValueError(f"OANDA client only allows Practice host: {OANDA_PRACTICE_HOST}")
        if not token:
            raise ValueError("OANDA Practice token is required")

        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "OandaPracticeClient":
        """从项目配置创建Practice客户端，调用方不直接读取令牌。"""
        settings = settings or get_settings()
        if (
            settings.oanda_practice_token is None
            or not settings.oanda_practice_token.get_secret_value()
        ):
            raise ValueError("OANDA Practice token is missing from .env")
        # get_secret_value只在构造Authorization请求头时解包，不进入普通输出。
        return cls(
            base_url=settings.oanda_practice_base_url,
            token=settings.oanda_practice_token.get_secret_value(),
        )

    def close(self) -> None:
        """关闭底层持久连接池。"""
        self._http.close()

    def __enter__(self) -> "OandaPracticeClient":
        """支持with调用，确保请求异常时也能释放连接。"""
        return self

    def __exit__(self, *_: object) -> None:
        # with代码块结束时调用close，不吞掉业务异常。
        self.close()

    def get_candlesticks(
        self,
        instrument: str,
        interval: str,
        *,
        limit: int = 1000,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> pd.DataFrame:
        """读取中间价K线并标准化为UTC升序DataFrame。"""
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"unsupported OANDA interval: {interval}")
        if not 1 <= limit <= 5000:
            raise ValueError("OANDA candlestick limit must be in [1, 5000]")
        range_requested = start_time is not None or end_time is not None
        if range_requested:
            if start_time is None or end_time is None:
                raise ValueError("start_time and end_time must be provided together")
            if start_time.tzinfo is None or end_time.tzinfo is None:
                raise ValueError("candlestick range must be timezone-aware")
            if start_time >= end_time:
                raise ValueError("start_time must be earlier than end_time")

        params: dict[str, object] = {
            "price": "M",
            "granularity": OANDA_GRANULARITY[interval],
            "smooth": "false",
        }
        if range_requested:
            # 调用范围模式时不发送count，遵守OANDA对count与from/to组合的约束。
            params["from"] = start_time.isoformat().replace("+00:00", "Z")
            params["to"] = end_time.isoformat().replace("+00:00", "Z")
        else:
            params["count"] = limit

        data = self._request_json(
            "GET", f"/v3/instruments/{instrument}/candles", params=params
        )
        candles = data.get("candles") if isinstance(data, dict) else None
        if not isinstance(candles, list):
            raise OandaApiError("OANDA candlestick response must contain a candle list")

        rows: list[dict[str, object]] = []
        for candle in candles:
            midpoint = candle.get("mid") if isinstance(candle, dict) else None
            if not isinstance(midpoint, dict):
                raise OandaApiError("OANDA midpoint candle data is missing")
            rows.append(
                {
                    "open_time": candle["time"],
                    "open": Decimal(str(midpoint["o"])),
                    "high": Decimal(str(midpoint["h"])),
                    "low": Decimal(str(midpoint["l"])),
                    "close": Decimal(str(midpoint["c"])),
                    "volume": Decimal(str(candle["volume"])),
                    "complete": bool(candle["complete"]),
                }
            )

        columns = ["open", "high", "low", "close", "volume", "complete"]
        if not rows:
            return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC"))
        frame = pd.DataFrame.from_records(rows)
        # OANDA返回RFC3339时间；调用pandas统一转换成UTC并按开盘时间去重排序。
        frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
        frame = frame.set_index("open_time").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        return frame[columns]

    def get_authorized_account_ids(self) -> tuple[str, ...]:
        """读取当前Token获准访问的Practice账户编号。"""
        data = self._request_json("GET", "/v3/accounts")
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list):
            raise OandaApiError("OANDA account list response is invalid")
        account_ids = tuple(
            str(account["id"])
            for account in accounts
            if isinstance(account, dict) and account.get("id")
        )
        if len(account_ids) != len(accounts):
            raise OandaApiError("OANDA account list contains an invalid account")
        return account_ids

    def get_account_summary(self, account_id: str) -> OandaPracticeAccount:
        """只读获取单个Practice账户的余额、净值和保证金摘要。"""
        if not account_id:
            raise ValueError("OANDA Practice account ID is required")
        data = self._request_json("GET", f"/v3/accounts/{account_id}/summary")
        account = data.get("account") if isinstance(data, dict) else None
        if not isinstance(account, dict):
            raise OandaApiError("OANDA account summary response is invalid")
        # 调用Decimal直接解析接口字符串，避免账户金额先经过float损失精度。
        return OandaPracticeAccount(
            account_id=str(account["id"]),
            currency=str(account["currency"]),
            balance=Decimal(str(account["balance"])),
            nav=Decimal(str(account["NAV"])),
            margin_available=Decimal(str(account["marginAvailable"])),
            margin_used=Decimal(str(account["marginUsed"])),
            unrealized_pl=Decimal(str(account["unrealizedPL"])),
            hedging_enabled=bool(account["hedgingEnabled"]),
        )

    def get_instrument_rules(
        self, account_id: str, instrument: str
    ) -> OandaInstrumentRules:
        """只读获取指定Practice账户可交易的品种规则。"""
        data = self._request_json(
            "GET",
            f"/v3/accounts/{account_id}/instruments",
            params={"instruments": instrument},
        )
        instruments = data.get("instruments") if isinstance(data, dict) else None
        if not isinstance(instruments, list) or len(instruments) != 1:
            raise OandaApiError("OANDA instrument rules response is invalid")
        rules = instruments[0]
        if not isinstance(rules, dict) or str(rules.get("name")) != instrument:
            raise OandaApiError("OANDA instrument rules do not match request")
        return OandaInstrumentRules(
            name=str(rules["name"]),
            display_precision=int(rules["displayPrecision"]),
            trade_units_precision=int(rules["tradeUnitsPrecision"]),
            minimum_trade_size=Decimal(str(rules["minimumTradeSize"])),
            maximum_order_units=Decimal(str(rules["maximumOrderUnits"])),
            maximum_position_size=Decimal(str(rules["maximumPositionSize"])),
            margin_rate=Decimal(str(rules["marginRate"])),
        )

    def get_price(self, account_id: str, instrument: str) -> OandaPrice:
        """只读获取指定品种当前最优买卖报价。"""
        data = self._request_json(
            "GET",
            f"/v3/accounts/{account_id}/pricing",
            params={"instruments": instrument},
        )
        prices = data.get("prices") if isinstance(data, dict) else None
        if not isinstance(prices, list) or len(prices) != 1:
            raise OandaApiError("OANDA pricing response is invalid")
        price = prices[0]
        if not isinstance(price, dict) or str(price.get("instrument")) != instrument:
            raise OandaApiError("OANDA price does not match request")
        return OandaPrice(
            instrument=instrument,
            bid=Decimal(str(price["closeoutBid"])),
            ask=Decimal(str(price["closeoutAsk"])),
            tradeable=bool(price.get("tradeable", False)),
        )

    def _request_json(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        """发送只读请求并把OANDA错误转换为不含令牌的异常。"""
        # 调用持久httpx客户端，Authorization由客户端默认请求头统一附加。
        response = self._http.request(method, path, params=params)
        try:
            data = response.json()
        except ValueError as exc:
            raise OandaApiError(
                f"OANDA returned non-JSON response: HTTP {response.status_code}"
            ) from exc
        if response.is_error:
            message = (
                data.get("errorMessage", "unknown error")
                if isinstance(data, dict)
                else "unknown error"
            )
            raise OandaApiError(f"OANDA API error {response.status_code}: {message}")
        return data
