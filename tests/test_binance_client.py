"""币安实盘公共行情客户端测试。"""

import json

import httpx
import pytest

from gold_crypto_quant.exchanges.binance import BinancePublicClient


def test_public_client_rejects_non_live_host() -> None:
    """只允许官方实盘公共域名，避免配置被替换到未知服务器。"""
    with pytest.raises(ValueError, match="only allows host"):
        BinancePublicClient(base_url="https://example.com")


def test_public_client_reads_klines_without_auth_headers() -> None:
    """K线调用不发送API密钥，并正确标准化币安数组格式。"""
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(
            200,
            content=json.dumps(
                [[1_788_320_000_000, "100", "102", "99", "101", "12", 0, "1212", 7, 0, 0]]
            ).encode(),
            headers={"Content-Type": "application/json"},
        )

    with BinancePublicClient(transport=httpx.MockTransport(handler)) as client:
        frame = client.get_candlesticks("BTCUSDT", "5m", limit=1)

    assert frame.iloc[0]["close"] == 101
    assert frame.iloc[0]["trade_count"] == 7
    assert all("x-mbx-apikey" not in headers for headers in seen_headers)


def test_exchange_symbol_validation_is_cached() -> None:
    """同一品种只下载一次合约目录。"""
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {"symbol": "ETHUSDT", "contractType": "PERPETUAL", "status": "TRADING"}
                ]
            },
        )

    with BinancePublicClient(transport=httpx.MockTransport(handler)) as client:
        client.get_exchange_symbol("ETHUSDT")
        client.get_exchange_symbol("ETHUSDT")

    assert request_count == 1


def test_exchange_symbol_accepts_tradifi_perpetual_gold_contract() -> None:
    """XAUUSDT等2026年新增的受监管黄金/白银合约标记为TRADIFI_PERPETUAL，同样应放行。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "XAUUSDT",
                        "contractType": "TRADIFI_PERPETUAL",
                        "status": "TRADING",
                    }
                ]
            },
        )

    with BinancePublicClient(transport=httpx.MockTransport(handler)) as client:
        match = client.get_exchange_symbol("XAUUSDT")

    assert match["symbol"] == "XAUUSDT"
