"""15分钟轨道轮转本地影子账户测试。"""

import json

import pandas as pd
import pytest

from gold_crypto_quant.runtime.bollinger_rotation_simulator import (
    run_rotation_paper_cycle,
)
from gold_crypto_quant.strategy import bollinger_range
from gold_crypto_quant.strategy.bollinger_range import build_rotation_box_context


@pytest.fixture(autouse=True)
def _disable_regime_filter(monkeypatch):
    """本文件测的是开仓与成交机制，用的是 [97, 103] 交替的合成数据（带宽 12%）。
    V5.9 的带宽上限会正确拒掉它，但那不是这些用例要验证的东西。"""
    monkeypatch.setattr(bollinger_range, "REGIME_FILTER_ENABLED", False)



def _rotation_bars(periods: int) -> pd.DataFrame:
    """构造三轨稳定、足够宽且最后一根可触发上轨的行情。"""
    index = pd.date_range("2026-01-01", periods=periods, freq="15min", tz="UTC")
    closes = ([97.0, 103.0] * ((periods + 1) // 2))[:periods]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [10.0] * periods,
            "quote_volume": [1000.0] * periods,
        },
        index=index,
    )


def test_shadow_simulator_starts_without_replaying_history(tmp_path) -> None:
    bars = _rotation_bars(40)
    state_path = tmp_path / "rotation.json"

    # 调用首次初始化，只设置最新K线游标，不应生成任何历史模拟成交。
    summary = run_rotation_paper_cycle(bars, state_path=state_path)

    assert summary.status == "INITIALIZED"
    assert summary.events == ()
    assert json.loads(state_path.read_text())["last_bar_time"] == bars.index[-1].isoformat()


def test_shadow_simulator_opens_short_on_new_upper_touch(tmp_path) -> None:
    bars = _rotation_bars(41)
    state_path = tmp_path / "rotation.json"
    # 先调用前40根完成初始化，模拟服务已经运行到上一根收盘。
    run_rotation_paper_cycle(bars.iloc[:40], state_path=state_path)
    context = build_rotation_box_context(bars)
    previous_upper = float(context.iloc[-2]["bb_upper"])
    bars.iloc[-1, bars.columns.get_loc("high")] = previous_upper + 1.0
    bars.iloc[-1, bars.columns.get_loc("low")] = max(
        float(bars.iloc[-1]["low"]), float(context.iloc[-2]["bb_lower"]) + 0.5
    )

    # 调用增量周期，本根只触上轨时应生成本地做空，不创建任何交易所订单。
    summary = run_rotation_paper_cycle(bars, state_path=state_path)

    assert summary.status == "RUNNING"
    assert summary.position_side == "SHORT"
    assert any("模拟开仓" in item.title for item in summary.events)
    assert summary.reason == "本地影子模拟；Gate订单提交接口未调用"
