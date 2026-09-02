"""Gate 测试网客户端的签名、解析和安全锁测试。"""

import hashlib
import hmac
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from gold_crypto_quant.exchanges.gate import GateTestnetClient

BASE_URL = "https://api-testnet.gateapi.io/api/v4"
API_KEY = "test-key"
API_SECRET = "test-secret"


def _mock_gate(request: httpx.Request) -> httpx.Response:
    """模拟 Gate 响应，并在私有接口处独立校验客户端签名。"""
    if request.url.path.endswith("/contracts/BTC_USDT"):
        return httpx.Response(
            200,
            json={
                "name": "BTC_USDT",
                "leverage_min": "1",
                "leverage_max": "125",
                "order_size_min": 1,
                "order_size_max": 1000,
                "quanto_multiplier": "0.0001",
                "enable_decimal": False,
                "order_price_round": "0.1",
                "mark_price": "65000.1",
                "index_price": "65001.2",
                "maker_fee_rate": "-0.00005",
                "taker_fee_rate": "0.0005",
                "in_delisting": False,
            },
        )
    if request.url.path.endswith("/candlesticks"):
        return httpx.Response(
            200,
            json=[
                {"t": 1000, "o": "10", "h": "13", "l": "9", "c": "12", "v": 2, "sum": "22"},
                {"t": 700, "o": "8", "h": "11", "l": "7", "c": "10", "v": 3, "sum": "27"},
            ],
        )
    if request.url.path.endswith("/accounts"):
        timestamp = request.headers["Timestamp"]
        payload_hash = hashlib.sha512(b"").hexdigest()
        signature_text = "\n".join(
            ("GET", "/api/v4/futures/usdt/accounts", "", payload_hash, timestamp)
        )
        # 测试端独立计算摘要，确认客户端没有遗漏路径、空查询串或空请求体哈希。
        expected = hmac.new(
            API_SECRET.encode(), signature_text.encode(), hashlib.sha512
        ).hexdigest()
        assert request.headers["KEY"] == API_KEY
        assert request.headers["SIGN"] == expected
        return httpx.Response(
            200,
            json={
                "currency": "USDT",
                "total": "10000",
                "available": "9000",
                "position_margin": "500",
                "order_margin": "100",
                "unrealised_pnl": "20",
                "in_dual_mode": True,
            },
        )
    return httpx.Response(404, json={"label": "NOT_FOUND", "message": "test route missing"})


@pytest.fixture
def client() -> GateTestnetClient:
    """每个测试使用独立 MockTransport，完全不会访问真实 Gate 网络。"""
    transport = httpx.MockTransport(_mock_gate)
    return GateTestnetClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        api_secret=API_SECRET,
        transport=transport,
    )


def test_live_host_is_rejected() -> None:
    # 即使调用方提供了密钥，传入实盘域名也必须在发送网络请求前失败。
    with pytest.raises(ValueError, match="only allows testnet"):
        GateTestnetClient(
            base_url="https://api.gateio.ws/api/v4",
            api_key=API_KEY,
            api_secret=API_SECRET,
        )


def test_contract_rules_are_parsed_as_decimal(client: GateTestnetClient) -> None:
    # 调用公开合约接口，验证杠杆和价格不会经过 float 丢失精度。
    contract = client.get_contract("BTC_USDT")
    assert contract.name == "BTC_USDT"
    assert contract.leverage_max == Decimal("125")
    assert contract.mark_price == Decimal("65000.1")
    assert contract.enable_decimal is False
    assert contract.order_price_round == Decimal("0.1")


def test_candlesticks_are_normalized_and_sorted(client: GateTestnetClient) -> None:
    # Gate 模拟响应故意按倒序返回，客户端必须整理成策略要求的时间升序。
    bars = client.get_candlesticks("BTC_USDT", "5m", limit=2)
    assert list(bars.columns) == ["open", "high", "low", "close", "volume", "quote_volume"]
    assert bars.index.is_monotonic_increasing
    assert str(bars.index.tz) == "UTC"
    assert bars.iloc[-1]["close"] == Decimal("12")


def test_one_minute_candlesticks_are_supported(client: GateTestnetClient) -> None:
    bars = client.get_candlesticks("ETH_USDT", "1m", limit=2)
    assert len(bars) == 2


def test_candlestick_range_uses_from_and_to_without_limit() -> None:
    """范围查询必须遵守Gate的from/to与limit互斥规则。"""
    captured_requests: list[httpx.Request] = []

    def capture_request(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json=[])

    range_client = GateTestnetClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        api_secret=API_SECRET,
        transport=httpx.MockTransport(capture_request),
    )
    try:
        # 调用范围查询，时间边界覆盖两个5分钟点。
        range_client.get_candlesticks(
            "BTC_USDT",
            "5m",
            start_time=datetime(2025, 1, 1, tzinfo=UTC),
            end_time=datetime(2025, 1, 1, 0, 5, tzinfo=UTC),
        )
    finally:
        # 调用close释放测试连接池。
        range_client.close()

    query = captured_requests[0].url.params
    assert query["from"] == "1735689600"
    assert query["to"] == "1735689900"
    assert "limit" not in query


def test_candlestick_range_rejects_more_than_2000_points(
    client: GateTestnetClient,
) -> None:
    """超过Gate单页上限的时间范围应在发送请求前拒绝。"""
    with pytest.raises(ValueError, match="cannot exceed 2000"):
        # 调用范围查询并故意传入2001个5分钟时间点。
        client.get_candlesticks(
            "BTC_USDT",
            "5m",
            start_time=datetime(2025, 1, 1, tzinfo=UTC),
            end_time=datetime(2025, 1, 7, 22, 40, tzinfo=UTC),
        )


def test_private_account_request_is_signed(client: GateTestnetClient) -> None:
    # MockTransport 会在返回账户前验证 KEY 与 HMAC 签名，因此这里同时覆盖私有认证。
    account = client.get_account()
    assert account.currency == "USDT"
    assert account.available == Decimal("9000")
    assert account.in_dual_mode is True
