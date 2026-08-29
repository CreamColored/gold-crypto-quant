"""交易策略模块，对外暴露 EMA 参数和信号生成函数。"""

from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters, generate_ema_signals
from gold_crypto_quant.strategy.multitimeframe_rsi import (
    MultiTimeframeRsiParameters,
    generate_multitimeframe_rsi_signals,
)

__all__ = [
    "EmaTrendParameters",
    "MultiTimeframeRsiParameters",
    "generate_ema_signals",
    "generate_multitimeframe_rsi_signals",
]
