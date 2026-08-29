"""vectorbt 回测模块。"""

from gold_crypto_quant.backtest.diagnostics import TradeDiagnostics, diagnose_trades
from gold_crypto_quant.backtest.ema_vectorbt import (
    EmaBacktestConfig,
    EmaBacktestResult,
    run_ema_backtest,
)
from gold_crypto_quant.backtest.research import (
    HoldoutResearchResult,
    InsufficientResearchData,
    RollingFoldResult,
    RollingResearchResult,
    run_holdout_research,
    run_rolling_research,
)

__all__ = [
    "EmaBacktestConfig",
    "EmaBacktestResult",
    "TradeDiagnostics",
    "diagnose_trades",
    "HoldoutResearchResult",
    "InsufficientResearchData",
    "RollingFoldResult",
    "RollingResearchResult",
    "run_holdout_research",
    "run_rolling_research",
    "run_ema_backtest",
]
