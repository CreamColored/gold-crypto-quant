"""双所对照路径的两家交易所适配器。

单独成模块是为了避开循环导入：live_refresh 复用 gate_history 的落库函数，因此
反过来不能由 gate_history 提供适配器。这里同时依赖两家的历史模块，方向单一。

步骤三会把这两个函数整体搬进采集服务，届时策略进程不再持有任何交易所客户端。
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from datetime import datetime

from sqlalchemy import Engine

from gold_crypto_quant.exchanges.binance import BinancePublicClient
from gold_crypto_quant.exchanges.gate import GatePublicClient
from gold_crypto_quant.market_data.binance_history import (
    INTERNAL_TO_BINANCE,
)
from gold_crypto_quant.market_data.binance_history import (
    _ensure_instrument as _ensure_binance_instrument,
)
from gold_crypto_quant.market_data.gate_history import (
    GATE_TESTNET_VENUE,
)
from gold_crypto_quant.market_data.gate_history import (
    _ensure_instrument as _ensure_gate_instrument,
)
from gold_crypto_quant.market_data.live_refresh import LiveRefreshResult, refresh_live_bars


def refresh_gate_live_bars(
    client: GatePublicClient,
    *,
    contracts: tuple[str, ...],
    intervals: tuple[str, ...],
    refreshed: MutableMapping[str, datetime],
    reporter: Callable[[str], None] | None = None,
    limit: int = 500,
    venue: str = GATE_TESTNET_VENUE,
    engine: Engine | None = None,
) -> list[LiveRefreshResult]:
    """Gate增量刷新，只拉到期周期。

    limit保持500而不是"只拉新增那几根"：单次往返约410毫秒且与数据量无关（实测
    limit=5和limit=1000都是410毫秒），多拉的成本是零，却能让服务中断后的缺口自动
    补齐，省掉一套补洞逻辑。
    """

    def check_contract(contract: str) -> None:
        if client.get_contract(contract).in_delisting:
            raise RuntimeError(f"Gate contract is delisting: {contract}")

    return refresh_live_bars(
        fetch=lambda contract, interval: client.get_candlesticks(contract, interval, limit=limit),
        ensure_instrument=lambda session, contract: _ensure_gate_instrument(
            session, contract, venue=venue
        ),
        check_contract=check_contract,
        contracts=contracts,
        intervals=intervals,
        refreshed=refreshed,
        reporter=reporter,
        engine=engine,
    )


def refresh_binance_live_bars(
    client: BinancePublicClient,
    *,
    contracts: tuple[str, ...],
    intervals: tuple[str, ...],
    refreshed: MutableMapping[str, datetime],
    reporter: Callable[[str], None] | None = None,
    limit: int = 500,
    engine: Engine | None = None,
) -> list[LiveRefreshResult]:
    """币安增量刷新；内部代码到交易所代码的映射在这一层完成。"""
    unknown = set(contracts).difference(INTERNAL_TO_BINANCE)
    if unknown:
        raise ValueError(f"unsupported Binance internal symbols: {sorted(unknown)}")

    return refresh_live_bars(
        fetch=lambda contract, interval: client.get_candlesticks(
            INTERNAL_TO_BINANCE[contract], interval, limit=limit
        ),
        ensure_instrument=lambda session, contract: _ensure_binance_instrument(session, contract),
        check_contract=lambda contract: client.get_exchange_symbol(INTERNAL_TO_BINANCE[contract]),
        contracts=contracts,
        intervals=intervals,
        refreshed=refreshed,
        reporter=reporter,
        engine=engine,
    )
