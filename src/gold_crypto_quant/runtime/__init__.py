"""长期运行服务和安全退出工具。"""

from gold_crypto_quant.runtime.bollinger_signal_cycle import (
    BollingerSignalCycleSummary,
    run_bollinger_signal_cycle,
)
from gold_crypto_quant.runtime.market_data_runner import (
    MARKET_DATA_SERVICE_NAME,
    OANDA_MARKET_DATA_SERVICE_NAME,
    MarketDataRunner,
    RunnerConfig,
    SingleInstanceLock,
    install_shutdown_signal_handlers,
)
from gold_crypto_quant.runtime.paper_signal_cycle import run_paper_signal_cycle

__all__ = [
    "MARKET_DATA_SERVICE_NAME",
    "OANDA_MARKET_DATA_SERVICE_NAME",
    "MarketDataRunner",
    "RunnerConfig",
    "SingleInstanceLock",
    "BollingerSignalCycleSummary",
    "install_shutdown_signal_handlers",
    "run_bollinger_signal_cycle",
    "run_paper_signal_cycle",
]
