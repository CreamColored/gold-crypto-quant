"""BTC和ETH共同突破参数研究测试。"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import gold_crypto_quant.backtest.research as research_module
from gold_crypto_quant.backtest import (
    EmaBacktestConfig,
    run_joint_breakout_research,
)
from gold_crypto_quant.strategy import EmaTrendParameters


def make_bars(count: int = 100) -> pd.DataFrame:
    """生成只用于研究边界的连续UTC行情。"""
    index = pd.date_range("2025-01-01", periods=count, freq="30min", tz="UTC")
    close = np.linspace(100.0, 120.0, count)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        },
        index=index,
    )


def test_joint_research_selects_parameter_that_works_for_both_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共同研究必须最大化较弱品种，而不是选择单个品种最高收益。"""

    def fake_backtest(
        _bars: pd.DataFrame,
        *,
        symbol: str,
        strategy: EmaTrendParameters,
        config: EmaBacktestConfig,
        trade_start: pd.Timestamp | None = None,
        **_kwargs: object,
    ) -> tuple[SimpleNamespace, None]:
        if trade_start is not None:
            result = SimpleNamespace(total_return=0.03, max_drawdown=0.01, trade_count=9)
            return result, None
        returns = {
            10: {"BTC_USDT": 0.20, "ETH_USDT": -0.10},
            20: {"BTC_USDT": 0.08, "ETH_USDT": 0.07},
            40: {"BTC_USDT": -0.02, "ETH_USDT": 0.15},
        }
        atr_penalty = abs(config.atr_multiple - 2.0) * 0.01
        result = SimpleNamespace(
            total_return=returns[strategy.pullback_lookback][symbol] - atr_penalty,
            max_drawdown=0.01,
            trade_count=12,
        )
        return result, None

    # 替换正式回测调用，用可控收益证明选择依据确实是两个品种中的较弱者。
    monkeypatch.setattr(research_module, "run_ema_backtest", fake_backtest)
    result = run_joint_breakout_research(
        {"BTC_USDT": make_bars(), "ETH_USDT": make_bars()},
        interval="30m",
        base_strategy=EmaTrendParameters(
            fast_period=3,
            slow_period=5,
            trend_period=8,
            entry_mode="breakout",
            pullback_lookback=20,
        ),
        base_config=EmaBacktestConfig(atr_period=2),
    )

    assert [fold.lookback for fold in result.folds] == [20, 20, 20]
    assert [fold.atr_multiple for fold in result.folds] == [2.0, 2.0, 2.0]
    assert result.stable_parameter_set
    assert result.positive_folds == {"BTC_USDT": 3, "ETH_USDT": 3}


def test_joint_research_requires_multiple_symbols() -> None:
    """单品种不能伪装成共同参数研究。"""
    with pytest.raises(ValueError, match="at least two symbols"):
        # 调用共同研究入口并故意只提供BTC，验证失败关闭。
        run_joint_breakout_research({"BTC_USDT": make_bars()}, interval="30m")
