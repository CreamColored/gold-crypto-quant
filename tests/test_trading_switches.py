"""交易开关的层级判定测试；纯内存计算，不触库。

关闭开关只阻止开仓，永远不会促成交易——已有仓位的止损、减仓与止盈照常执行。
"""

from gold_crypto_quant.storage.trading_switches import (
    GLOBAL_SCOPE,
    resolve_entry_allowed,
    symbol_scope,
)

VENUE = "GATE_LIVE_PUBLIC"
SYMBOL = "BTC_USDT"
KEY = symbol_scope(VENUE, SYMBOL)


def test_missing_switches_default_to_enabled() -> None:
    """没有记录按开启处理。

    新接入的交易所或品种不该因为还没建记录就被静默禁用——那样故障会以
    "策略莫名不开单"的形式出现，很难排查。禁用必须是一次显式写入。
    """
    assert resolve_entry_allowed({}, VENUE, SYMBOL) is True


def test_each_level_can_block_on_its_own() -> None:
    """三级中任意一级关闭都足以禁止开仓。"""
    assert resolve_entry_allowed({GLOBAL_SCOPE: False}, VENUE, SYMBOL) is False
    assert resolve_entry_allowed({VENUE: False}, VENUE, SYMBOL) is False
    assert resolve_entry_allowed({KEY: False}, VENUE, SYMBOL) is False


def test_lower_level_cannot_override_a_closed_parent() -> None:
    """品种开着但交易所关着，仍然禁止——下级不能反选上级。"""
    switches = {GLOBAL_SCOPE: True, VENUE: False, KEY: True}

    assert resolve_entry_allowed(switches, VENUE, SYMBOL) is False


def test_switches_are_scoped_per_venue() -> None:
    """关掉一个交易所不能波及另一个；双所对照要求两边互不影响。"""
    switches = {"GATE_LIVE_PUBLIC": False}

    assert resolve_entry_allowed(switches, "GATE_LIVE_PUBLIC", SYMBOL) is False
    assert resolve_entry_allowed(switches, "BINANCE_LIVE_PUBLIC", SYMBOL) is True


def test_symbol_scope_key_format() -> None:
    assert symbol_scope("BINANCE_LIVE_PUBLIC", "ETH_USDT") == "BINANCE_LIVE_PUBLIC:ETH_USDT"
