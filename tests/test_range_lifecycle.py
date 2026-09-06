import pandas as pd

from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (
    _capped_risk_quantity,
    _new_state,
    _refresh_box_qualification,
)
from gold_crypto_quant.strategy.bollinger_range import BollingerRangeParameters


def row(time: str, *, close=100.0, evidence=0.8, risk=0.2, candidate=True):
    return pd.Series(
        {
            "box_candidate": candidate,
            "breakout": False,
            "range_evidence_score": evidence,
            "breakout_risk_score": risk,
            "bb_upper": 110.0,
            "bb_middle": 100.0,
            "bb_lower": 90.0,
            "close": close,
        },
        name=pd.Timestamp(time, tz="UTC"),
    )


def test_lifecycle_requires_confirmation_and_freezes_levels():
    state = _new_state(10_000, ("ETH_USDT",))
    parameters = BollingerRangeParameters(lifecycle_confirmation_bars=2)
    _refresh_box_qualification(
        state, "ETH_USDT", "15m", context_row=row("2026-01-01 00:00"), parameters=parameters
    )
    assert state.box_active["ETH_USDT"]["15m"] is False
    _refresh_box_qualification(
        state, "ETH_USDT", "15m", context_row=row("2026-01-01 00:15"), parameters=parameters
    )
    assert state.box_active["ETH_USDT"]["15m"] is True
    assert state.box_upper["ETH_USDT"]["15m"] == 110.0
    assert state.box_lower["ETH_USDT"]["15m"] == 90.0


def test_lifecycle_keeps_fixed_levels_then_breaks_on_close_outside_buffer():
    state = _new_state(10_000, ("ETH_USDT",))
    parameters = BollingerRangeParameters(lifecycle_confirmation_bars=1, maximum_band_drift=2.5)
    _refresh_box_qualification(
        state, "ETH_USDT", "15m", context_row=row("2026-01-01 00:00"), parameters=parameters
    )
    shifted = row("2026-01-01 00:15")
    shifted[["bb_upper", "bb_middle", "bb_lower"]] = [120.0, 110.0, 100.0]
    _refresh_box_qualification(
        state, "ETH_USDT", "15m", context_row=shifted, parameters=parameters
    )
    assert state.box_upper["ETH_USDT"]["15m"] == 110.0
    _refresh_box_qualification(
        state,
        "ETH_USDT",
        "15m",
        context_row=row("2026-01-01 00:30", close=113.0),
        parameters=parameters,
    )
    assert state.box_active["ETH_USDT"]["15m"] is False


def test_high_breakout_risk_does_not_form_new_box():
    state = _new_state(10_000, ("ETH_USDT",))
    parameters = BollingerRangeParameters(lifecycle_confirmation_bars=1)
    _refresh_box_qualification(
        state,
        "ETH_USDT",
        "15m",
        context_row=row("2026-01-01 00:00", risk=0.75),
        parameters=parameters,
    )
    assert state.box_active["ETH_USDT"]["15m"] is False


def test_tighter_structural_stop_does_not_increase_position_size():
    # 25U风险预算、旧固定止损每单位亏12.5U时，基准仓位最多2个单位。
    # 新箱体止损即使缩到每单位3.5U，也只能降低实亏，不能把仓位放大到7.14。
    quantity = _capped_risk_quantity(
        risk_budget=25.0,
        actual_loss_per_unit=3.5,
        reference_loss_per_unit=12.5,
    )
    assert quantity == 2.0
    assert quantity * 3.5 == 7.0
