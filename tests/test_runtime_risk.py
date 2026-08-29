"""账户级每日亏损和最大回撤状态机测试。"""

from datetime import date
from decimal import Decimal

from gold_crypto_quant.risk.runtime import RuntimeRiskStatus, evaluate_runtime_risk


def evaluate(
    current: str,
    day_start: str = "10000",
    peak: str = "10000",
    **changes: object,
):
    """使用字符串构造精确Decimal测试权益。"""
    values = {
        "trading_day": date(2025, 1, 2),
        "current_equity": Decimal(current),
        "day_start_equity": Decimal(day_start),
        "historical_peak_equity": Decimal(peak),
    }
    values.update(changes)
    return evaluate_runtime_risk(**values)  # type: ignore[arg-type]


def test_normal_equity_allows_orders() -> None:
    """未达到任何亏损阈值时状态应为NORMAL。"""
    # 调用状态机评估小幅盈利账户。
    result = evaluate("10100")
    assert result.state == RuntimeRiskStatus.NORMAL


def test_daily_loss_limit_halts_for_entire_day() -> None:
    """当日亏损达到2%后，即使权益恢复也不能在当天重新开仓。"""
    # 第一次调用状态机，在精确-2%位置触发当日熔断。
    halted = evaluate("9800")
    assert halted.state == RuntimeRiskStatus.DAILY_HALTED
    # 第二次调用模拟权益恢复，并传入当天已经触发的持久化状态。
    recovered = evaluate(
        "10050",
        previous_state=RuntimeRiskStatus.DAILY_HALTED,
        previous_trading_day=date(2025, 1, 2),
    )
    assert recovered.state == RuntimeRiskStatus.DAILY_HALTED


def test_daily_halt_resets_on_next_utc_day() -> None:
    """新的UTC交易日且权益正常时可以解除昨日每日熔断。"""
    # 调用状态机并传入前一日DAILY_HALTED，当前日收益正常应恢复NORMAL。
    result = evaluate(
        "10000",
        previous_state=RuntimeRiskStatus.DAILY_HALTED,
        previous_trading_day=date(2025, 1, 1),
    )
    assert result.state == RuntimeRiskStatus.NORMAL


def test_drawdown_halt_has_priority_and_is_permanent() -> None:
    """最大回撤达到8%后，次日和权益反弹都不能自动解除。"""
    # 当前权益相对11000峰值回撤超过8%，应触发永久熔断。
    halted = evaluate("10000", day_start="10500", peak="11000")
    assert halted.state == RuntimeRiskStatus.DRAWDOWN_HALTED
    # 调用状态机模拟次日反弹，上一状态仍强制保持DRAWDOWN_HALTED。
    next_day = evaluate(
        "10900",
        day_start="10800",
        peak="11000",
        trading_day=date(2025, 1, 3),
        previous_state=RuntimeRiskStatus.DRAWDOWN_HALTED,
        previous_trading_day=date(2025, 1, 2),
    )
    assert next_day.state == RuntimeRiskStatus.DRAWDOWN_HALTED


def test_zero_or_negative_equity_triggers_drawdown_instead_of_crashing() -> None:
    # 爆仓后的零或负权益仍需形成永久回撤熔断，不能因校验异常绕过风控状态写入。
    assert evaluate("0").state == RuntimeRiskStatus.DRAWDOWN_HALTED
    assert evaluate("-100").state == RuntimeRiskStatus.DRAWDOWN_HALTED
