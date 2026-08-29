"""账户级每日亏损和最大回撤运行时状态机。"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class RuntimeRiskStatus(StrEnum):
    """订单入口识别的三种账户风控状态。"""

    NORMAL = "NORMAL"
    DAILY_HALTED = "DAILY_HALTED"
    DRAWDOWN_HALTED = "DRAWDOWN_HALTED"


@dataclass(frozen=True, slots=True)
class RuntimeRiskEvaluation:
    """一次账户权益快照对应的不可变风控计算结果。"""

    state: RuntimeRiskStatus
    trading_day: date
    day_start_equity: Decimal
    peak_equity: Decimal
    latest_equity: Decimal
    daily_return: Decimal
    drawdown: Decimal
    reason: str


def evaluate_runtime_risk(
    *,
    trading_day: date,
    current_equity: Decimal,
    day_start_equity: Decimal,
    historical_peak_equity: Decimal,
    daily_loss_limit: Decimal = Decimal("0.02"),
    max_drawdown_limit: Decimal = Decimal("0.08"),
    previous_state: RuntimeRiskStatus | None = None,
    previous_trading_day: date | None = None,
) -> RuntimeRiskEvaluation:
    """计算账户风控状态；总回撤熔断永久保持，每日熔断到次日才重置。"""
    if day_start_equity <= 0 or historical_peak_equity <= 0:
        raise ValueError("account equity denominators must be positive")
    if not Decimal("0") < daily_loss_limit < max_drawdown_limit < Decimal("1"):
        raise ValueError("risk limits must satisfy 0 < daily < drawdown < 1")

    peak_equity = max(historical_peak_equity, current_equity)
    daily_return = current_equity / day_start_equity - Decimal("1")
    drawdown = current_equity / peak_equity - Decimal("1")

    if previous_state == RuntimeRiskStatus.DRAWDOWN_HALTED:
        state = RuntimeRiskStatus.DRAWDOWN_HALTED
        reason = "最大回撤熔断已经触发，必须人工审核后才能解除"
    elif drawdown <= -max_drawdown_limit:
        state = RuntimeRiskStatus.DRAWDOWN_HALTED
        reason = f"账户最大回撤{drawdown:.2%}达到{max_drawdown_limit:.2%}限制"
    elif previous_state == RuntimeRiskStatus.DAILY_HALTED and previous_trading_day == trading_day:
        state = RuntimeRiskStatus.DAILY_HALTED
        reason = "当日亏损熔断已经触发，UTC次日之前禁止新开仓"
    elif daily_return <= -daily_loss_limit:
        state = RuntimeRiskStatus.DAILY_HALTED
        reason = f"账户当日收益{daily_return:.2%}达到-{daily_loss_limit:.2%}限制"
    else:
        state = RuntimeRiskStatus.NORMAL
        reason = "账户权益处于允许开仓的风控范围"

    return RuntimeRiskEvaluation(
        state=state,
        trading_day=trading_day,
        day_start_equity=day_start_equity,
        peak_equity=peak_equity,
        latest_equity=current_equity,
        daily_return=daily_return,
        drawdown=drawdown,
        reason=reason,
    )
