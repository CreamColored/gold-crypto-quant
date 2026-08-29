"""行情采集与持久化模块。"""

from gold_crypto_quant.market_data.gate_history import import_gate_history
from gold_crypto_quant.market_data.oanda_history import import_oanda_history

__all__ = ["import_gate_history", "import_oanda_history"]
