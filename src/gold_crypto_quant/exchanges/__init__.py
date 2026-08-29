"""交易所适配模块，仅允许连接官方测试网或模拟盘。"""

from gold_crypto_quant.exchanges.gate import GateTestnetClient
from gold_crypto_quant.exchanges.oanda import OandaPracticeClient

__all__ = ["GateTestnetClient", "OandaPracticeClient"]
