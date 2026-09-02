"""Gate USDT 永续合约测试网只读客户端。

第一版只开放合约信息、历史 K 线和账户查询，不提供下单方法。这样可以先验证行情、
签名和数据标准化，再接入订单幂等与风控，避免尚未完成风控时误发交易请求。
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import pandas as pd

from gold_crypto_quant.config import Settings, get_settings

# 1分钟仅用于主策略的微周期过滤；3分钟由连续1分钟K线本地聚合。
SUPPORTED_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "1h"})
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
GATE_TESTNET_HOST = "api-testnet.gateapi.io"
GATE_LIVE_HOSTS = frozenset({"api.gateio.ws", "fx-api.gateio.ws"})


class GateApiError(RuntimeError):
    """Gate 返回非成功状态或不符合预期的数据结构。"""


@dataclass(frozen=True, slots=True)
class GateContract:
    """策略和仓位计算需要的合约规则。"""

    name: str
    leverage_min: Decimal
    leverage_max: Decimal
    order_size_min: Decimal
    order_size_max: Decimal
    quanto_multiplier: Decimal
    enable_decimal: bool
    order_price_round: Decimal
    mark_price: Decimal
    index_price: Decimal
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    in_delisting: bool


@dataclass(frozen=True, slots=True)
class GateFuturesAccount:
    """USDT 合约账户的核心余额和保证金字段。"""

    currency: str
    total: Decimal
    available: Decimal
    position_margin: Decimal
    order_margin: Decimal
    unrealised_pnl: Decimal
    in_dual_mode: bool


class GateTestnetClient:
    """封装 Gate API V4 测试网的公开查询和私有签名请求。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed_url = urlsplit(base_url)
        # 安全锁：即使 .env 被误改，也不允许这个客户端连接 Gate 实盘域名。
        if parsed_url.hostname != GATE_TESTNET_HOST:
            raise ValueError(f"Gate client only allows testnet host: {GATE_TESTNET_HOST}")
        if not api_key or not api_secret:
            raise ValueError("Gate testnet API key and secret are required")

        self._base_url = base_url.rstrip("/")
        self._api_prefix = parsed_url.path.rstrip("/") or "/api/v4"
        self._api_key = api_key
        self._api_secret = api_secret.encode()
        # 复用一个 httpx.Client 可以复用 TCP/TLS 连接，减少多周期轮询的连接开销。
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "GateTestnetClient":
        """从项目配置创建客户端，调用方无需直接读取或传递密钥。"""
        # 正常运行使用缓存配置；测试可以传入独立 Settings，避免污染全局状态。
        settings = settings or get_settings()
        if settings.gate_testnet_api_key is None or settings.gate_testnet_api_secret is None:
            raise ValueError("Gate testnet credentials are missing from .env")

        # get_secret_value() 只在构造请求客户端这一处解包，其他日志仍显示掩码。
        return cls(
            base_url=settings.gate_testnet_base_url,
            api_key=settings.gate_testnet_api_key.get_secret_value(),
            api_secret=settings.gate_testnet_api_secret.get_secret_value(),
        )

    def close(self) -> None:
        """关闭底层连接池；长期服务退出时必须调用。"""
        self._http.close()

    def __enter__(self) -> "GateTestnetClient":
        """支持 ``with`` 调用，确保异常发生时也能释放连接。"""
        return self

    def __exit__(self, *_: object) -> None:
        # with 代码块结束时统一调用 close，不吞掉调用方产生的异常。
        self.close()

    def get_contract(self, contract: str, settle: str = "usdt") -> GateContract:
        """查询单个永续合约的杠杆、下单数量和费率规则。"""
        # 该接口无需签名，适合启动时校验 BTC_USDT、ETH_USDT 是否仍可交易。
        data = self._request_json("GET", f"/futures/{settle}/contracts/{contract}")
        if not isinstance(data, dict):
            raise GateApiError("Gate contract response must be an object")

        # Decimal 直接读取接口字符串，避免先转 float 造成精度损失。
        return GateContract(
            name=str(data["name"]),
            leverage_min=Decimal(str(data["leverage_min"])),
            leverage_max=Decimal(str(data["leverage_max"])),
            order_size_min=Decimal(str(data["order_size_min"])),
            order_size_max=Decimal(str(data["order_size_max"])),
            quanto_multiplier=Decimal(str(data["quanto_multiplier"])),
            enable_decimal=bool(data.get("enable_decimal", False)),
            order_price_round=Decimal(str(data.get("order_price_round", "0"))),
            mark_price=Decimal(str(data["mark_price"])),
            index_price=Decimal(str(data["index_price"])),
            maker_fee_rate=Decimal(str(data["maker_fee_rate"])),
            taker_fee_rate=Decimal(str(data["taker_fee_rate"])),
            in_delisting=bool(data["in_delisting"]),
        )

    def get_candlesticks(
        self,
        contract: str,
        interval: str,
        *,
        limit: int = 1000,
        settle: str = "usdt",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> pd.DataFrame:
        """查询历史 K 线并转换为策略统一使用的 DataFrame。"""
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"unsupported Gate interval: {interval}")
        if not 1 <= limit <= 2000:
            raise ValueError("Gate candlestick limit must be in [1, 2000]")

        range_requested = start_time is not None or end_time is not None
        if range_requested:
            if start_time is None or end_time is None:
                raise ValueError("start_time and end_time must be provided together")
            if start_time.tzinfo is None or end_time.tzinfo is None:
                raise ValueError("candlestick range must be timezone-aware")
            if start_time >= end_time:
                raise ValueError("start_time must be earlier than end_time")
            point_count = (
                int((end_time - start_time).total_seconds()) // INTERVAL_SECONDS[interval] + 1
            )
            if point_count > 2000:
                raise ValueError("Gate candlestick time range cannot exceed 2000 points")

        params: dict[str, object] = {"contract": contract, "interval": interval}
        if range_requested:
            # Gate官方规定from/to与limit互斥，因此范围查询只发送Unix秒时间边界。
            params["from"] = int(start_time.timestamp())
            params["to"] = int(end_time.timestamp())
        else:
            params["limit"] = limit

        # 参数顺序保持固定，签名接口扩展时也能得到稳定的查询字符串顺序。
        data = self._request_json(
            "GET",
            f"/futures/{settle}/candlesticks",
            params=params,
        )
        if not isinstance(data, list):
            raise GateApiError("Gate candlestick response must be a list")

        rows = []
        for candle in data:
            if not isinstance(candle, dict):
                raise GateApiError("Gate candlestick item must be an object")
            rows.append(
                {
                    "open_time": int(candle["t"]),
                    # 行情层保留 Decimal 精度；EMA 策略入口会在计算前显式转换成 float。
                    "open": Decimal(str(candle["o"])),
                    "high": Decimal(str(candle["h"])),
                    "low": Decimal(str(candle["l"])),
                    "close": Decimal(str(candle["c"])),
                    "volume": Decimal(str(candle["v"])),
                    "quote_volume": Decimal(str(candle["sum"])),
                }
            )

        columns = ["open", "high", "low", "close", "volume", "quote_volume"]
        if not rows:
            # 返回结构固定的空表，让上层可以统一拼接而无需判断 None。
            return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC"))

        frame = pd.DataFrame.from_records(rows)
        # Gate 时间戳是 Unix 秒；转换成 UTC 索引后排序并去重，满足策略入口要求。
        frame["open_time"] = pd.to_datetime(frame["open_time"], unit="s", utc=True)
        frame = frame.set_index("open_time").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        return frame[columns]

    def get_account(self, settle: str = "usdt") -> GateFuturesAccount:
        """签名查询测试网合约账户，不执行任何账户或仓位修改。"""
        data = self._request_json("GET", f"/futures/{settle}/accounts", private=True)
        if not isinstance(data, dict):
            raise GateApiError("Gate account response must be an object")

        return GateFuturesAccount(
            currency=str(data["currency"]),
            total=Decimal(str(data["total"])),
            available=Decimal(str(data["available"])),
            position_margin=Decimal(str(data["position_margin"])),
            order_margin=Decimal(str(data["order_margin"])),
            unrealised_pnl=Decimal(str(data["unrealised_pnl"])),
            in_dual_mode=bool(data["in_dual_mode"]),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
    ) -> Any:
        """发送请求、检查 HTTP 状态，并解析 Gate JSON 错误。"""
        params = params or {}
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers: dict[str, str] = {}
        if private:
            # 私有接口在发送前本地计算 HMAC-SHA512；Secret 本身不会传给 Gate。
            headers.update(self._build_auth_headers(method, path, params, body_text))

        # content 使用与签名完全一致的紧凑 JSON 字符串，避免序列化差异造成验签失败。
        response = self._http.request(
            method,
            path,
            params=params,
            content=body_text or None,
            headers=headers,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise GateApiError(
                f"Gate returned non-JSON response: HTTP {response.status_code}"
            ) from exc

        if response.is_error:
            label = data.get("label", "UNKNOWN") if isinstance(data, dict) else "UNKNOWN"
            message = data.get("message", "unknown error") if isinstance(data, dict) else str(data)
            raise GateApiError(f"Gate API error {response.status_code} [{label}]: {message}")
        return data

    def _build_auth_headers(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        body_text: str,
        *,
        timestamp: str | None = None,
    ) -> dict[str, str]:
        """按照 Gate API V4 规范生成 KEY、Timestamp 和 SIGN 请求头。"""
        timestamp = timestamp or str(int(time.time()))
        query_string = urlencode(params)
        payload_hash = hashlib.sha512(body_text.encode()).hexdigest()
        request_path = f"{self._api_prefix}{path}"
        signature_text = "\n".join(
            (method.upper(), request_path, query_string, payload_hash, timestamp)
        )
        # HMAC 使用 Secret 作为本地签名密钥，只把摘要 SIGN 发送给服务器。
        signature = hmac.new(
            self._api_secret,
            signature_text.encode(),
            hashlib.sha512,
        ).hexdigest()
        return {"KEY": self._api_key, "Timestamp": timestamp, "SIGN": signature}


class GatePublicClient(GateTestnetClient):
    """Gate实盘USDT永续只读公共行情客户端，不接受或保存任何API密钥。"""

    def __init__(
        self,
        *,
        base_url: str = "https://api.gateio.ws/api/v4",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed_url = urlsplit(base_url)
        if parsed_url.hostname not in GATE_LIVE_HOSTS:
            raise ValueError(
                f"Gate public client only allows live hosts: {sorted(GATE_LIVE_HOSTS)}"
            )
        self._base_url = base_url.rstrip("/")
        self._api_prefix = parsed_url.path.rstrip("/") or "/api/v4"
        # 公共行情请求永远不会调用签名；保留空值只是复用父类的只读解析实现。
        self._api_key = ""
        self._api_secret = b""
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def get_account(self, settle: str = "usdt") -> GateFuturesAccount:
        """公共客户端明确拒绝私有账户访问，防止未来误把它扩展成实盘交易客户端。"""
        del settle
        raise PermissionError("Gate public market client cannot access private accounts")
