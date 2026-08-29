"""本地模拟资金账本的纯计算规则。"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PaperAccountEvaluation:
    """根据初始资金、成交和持仓汇总得到的账户口径。"""

    balance: Decimal
    equity: Decimal
    available_margin: Decimal
    used_margin: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fees: Decimal


def calculate_paper_account(
    *,
    initial_equity: Decimal,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    total_fees: Decimal,
    used_margin: Decimal,
) -> PaperAccountEvaluation:
    """按模拟成交总账计算余额、权益和可用保证金。"""
    if initial_equity <= 0:
        raise ValueError("paper initial equity must be positive")
    if total_fees < 0 or used_margin < 0:
        raise ValueError("paper fees and used margin cannot be negative")
    balance = initial_equity + realized_pnl - total_fees
    equity = balance + unrealized_pnl
    available_margin = max(equity - used_margin, Decimal("0"))
    return PaperAccountEvaluation(
        balance=balance,
        equity=equity,
        available_margin=available_margin,
        used_margin=used_margin,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_fees=total_fees,
    )
