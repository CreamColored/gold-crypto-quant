"""OANDA Practice只读行情客户端测试。"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from gold_crypto_quant.exchanges.oanda import (
    OandaPracticeClient,
    resolve_oanda_account_id,
)

BASE_URL = "https://api-fxpractice.oanda.com"
TOKEN = "test-practice-token"


def _mock_oanda(request: httpx.Request) -> httpx.Response:
    """模拟官方蜡烛响应并验证Bearer认证与Practice路径。"""
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.url.path == "/v3/instruments/XAU_USD/candles"
    return httpx.Response(
        200,
        json={
            "instrument": "XAU_USD",
            "granularity": "M5",
            "candles": [
                {
                    "time": "2026-08-18T10:05:00.000000000Z",
                    "mid": {
                        "o": "2501.1",
                        "h": "2503.2",
                        "l": "2500.4",
                        "c": "2502.8",
                    },
                    "volume": 25,
                    "complete": True,
                },
                {
                    "time": "2026-08-18T10:00:00.000000000Z",
                    "mid": {
                        "o": "2500.1",
                        "h": "2502.0",
                        "l": "2499.5",
                        "c": "2501.1",
                    },
                    "volume": 20,
                    "complete": True,
                },
            ],
        },
    )


def test_live_host_is_rejected() -> None:
    # 即使令牌有效，也必须在网络请求前拒绝OANDA实盘域名。
    with pytest.raises(ValueError, match="only allows Practice"):
        OandaPracticeClient(base_url="https://api-fxtrade.oanda.com", token=TOKEN)


def test_non_https_or_extra_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="only allows Practice"):
        OandaPracticeClient(base_url="http://api-fxpractice.oanda.com", token=TOKEN)
    with pytest.raises(ValueError, match="only allows Practice"):
        OandaPracticeClient(base_url=f"{BASE_URL}/v3", token=TOKEN)


def test_candlesticks_are_authenticated_normalized_and_sorted() -> None:
    client = OandaPracticeClient(
        base_url=BASE_URL,
        token=TOKEN,
        transport=httpx.MockTransport(_mock_oanda),
    )
    try:
        # 调用只读蜡烛方法，测试端会独立检查Practice路径和Bearer请求头。
        bars = client.get_candlesticks("XAU_USD", "5m", limit=2)
    finally:
        client.close()

    assert bars.index.is_monotonic_increasing
    assert str(bars.index.tz) == "UTC"
    assert bars.iloc[-1]["close"] == Decimal("2502.8")
    assert bool(bars.iloc[-1]["complete"])


def test_range_query_uses_from_and_to_without_count() -> None:
    captured: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"candles": []})

    client = OandaPracticeClient(
        base_url=BASE_URL,
        token=TOKEN,
        transport=httpx.MockTransport(capture),
    )
    try:
        # 调用范围查询，客户端应使用from/to而不是同时发送count。
        client.get_candlesticks(
            "XAU_USD",
            "1h",
            start_time=datetime(2026, 1, 1, tzinfo=UTC),
            end_time=datetime(2026, 1, 2, tzinfo=UTC),
        )
    finally:
        client.close()

    query = captured[0].url.params
    assert query["granularity"] == "H1"
    assert query["from"] == "2026-01-01T00:00:00Z"
    assert query["to"] == "2026-01-02T00:00:00Z"
    assert "count" not in query


def test_authorized_accounts_and_summary_are_read_only_decimals() -> None:
    def account_routes(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/v3/accounts":
            return httpx.Response(200, json={"accounts": [{"id": "101-001-123-001"}]})
        if request.url.path == "/v3/accounts/101-001-123-001/summary":
            return httpx.Response(
                200,
                json={
                    "account": {
                        "id": "101-001-123-001",
                        "currency": "USD",
                        "balance": "10000.12",
                        "NAV": "10020.34",
                        "marginAvailable": "9800.11",
                        "marginUsed": "220.23",
                        "unrealizedPL": "20.22",
                        "hedgingEnabled": True,
                    }
                },
            )
        return httpx.Response(404, json={"errorMessage": "missing test route"})

    client = OandaPracticeClient(
        base_url=BASE_URL,
        token=TOKEN,
        transport=httpx.MockTransport(account_routes),
    )
    try:
        # 调用授权账户发现后再读取摘要，全程只允许GET请求。
        account_ids = client.get_authorized_account_ids()
        account = client.get_account_summary(account_ids[0])
    finally:
        client.close()

    assert account_ids == ("101-001-123-001",)
    assert account.nav == Decimal("10020.34")
    assert account.margin_used == Decimal("220.23")
    assert account.hedging_enabled is True


def test_instrument_rules_and_price_are_read_only_decimals() -> None:
    def instrument_routes(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.params["instruments"] == "XAU_USD"
        if request.url.path.endswith("/instruments"):
            return httpx.Response(
                200,
                json={
                    "instruments": [
                        {
                            "name": "XAU_USD",
                            "displayPrecision": 3,
                            "tradeUnitsPrecision": 0,
                            "minimumTradeSize": "1",
                            "maximumOrderUnits": "10000",
                            "maximumPositionSize": "0",
                            "marginRate": "0.05",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/pricing"):
            return httpx.Response(
                200,
                json={
                    "prices": [
                        {
                            "instrument": "XAU_USD",
                            "closeoutBid": "2499.125",
                            "closeoutAsk": "2499.375",
                            "tradeable": True,
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"errorMessage": "missing test route"})

    client = OandaPracticeClient(
        base_url=BASE_URL,
        token=TOKEN,
        transport=httpx.MockTransport(instrument_routes),
    )
    try:
        # 调用品种规则和当前报价，两条路径都必须保持GET且返回Decimal。
        rules = client.get_instrument_rules("101-001-123-001", "XAU_USD")
        price = client.get_price("101-001-123-001", "XAU_USD")
    finally:
        client.close()

    assert rules.margin_rate == Decimal("0.05")
    assert rules.trade_units_precision == 0
    assert price.bid == Decimal("2499.125")
    assert price.ask == Decimal("2499.375")
    assert price.tradeable is True


def test_account_resolution_rejects_unauthorized_configuration() -> None:
    class AccountListClient:
        def get_authorized_account_ids(self) -> tuple[str, ...]:
            return ("practice-account",)

    client = AccountListClient()
    # 调用公共账户解析方法，单账户可自动选择，错误配置则必须失败关闭。
    assert resolve_oanda_account_id(client, None) == ("practice-account", True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not authorized"):
        resolve_oanda_account_id(client, "wrong-account")  # type: ignore[arg-type]
