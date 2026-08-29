"""策略进入模拟交易前的强制样本外准入规则。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from gold_crypto_quant.backtest.research import RollingResearchResult


@dataclass(frozen=True, slots=True)
class QualificationCriteria:
    """第一版策略准入门槛，所有条件必须同时满足。"""

    minimum_compounded_return: float = 0.0
    maximum_worst_drawdown: float = 0.08
    minimum_positive_folds: int = 2
    minimum_trades_per_fold: int = 8

    def __post_init__(self) -> None:
        if self.minimum_compounded_return < 0:
            raise ValueError("minimum_compounded_return cannot be negative")
        if not 0 < self.maximum_worst_drawdown <= 0.25:
            raise ValueError("maximum_worst_drawdown must be in (0, 0.25]")
        if self.minimum_positive_folds < 1 or self.minimum_trades_per_fold < 1:
            raise ValueError("fold and trade minimums must be positive")


@dataclass(frozen=True, slots=True)
class QualificationDecision:
    """不可变的策略准入结论及完整审计内容。"""

    evaluation_hash: str
    strategy_name: str
    strategy_version: str
    symbol: str
    interval: str
    approved: bool
    compounded_return: float
    worst_drawdown: float
    positive_folds: int
    total_folds: int
    minimum_fold_trades: int
    criteria: dict[str, object]
    details: dict[str, object]
    reason: str
    evaluated_at: datetime


def evaluate_rolling_research(
    research: RollingResearchResult,
    *,
    strategy_name: str = "EMA_TREND",
    strategy_version: str = "1.0.0",
    criteria: QualificationCriteria | None = None,
    evaluated_at: datetime | None = None,
) -> QualificationDecision:
    """依据滚动样本外结果生成批准或拒绝决定。"""
    criteria = criteria or QualificationCriteria()
    evaluated_at = evaluated_at or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")

    fold_trade_counts = [fold.research.selected_test_result.trade_count for fold in research.folds]
    minimum_fold_trades = min(fold_trade_counts)
    failures: list[str] = []
    if research.selected_compounded_return <= criteria.minimum_compounded_return:
        failures.append(
            f"样本外复合收益{research.selected_compounded_return:.2%}未高于"
            f"{criteria.minimum_compounded_return:.2%}"
        )
    if research.selected_worst_drawdown >= criteria.maximum_worst_drawdown:
        failures.append(
            f"最差回撤{research.selected_worst_drawdown:.2%}达到或超过"
            f"{criteria.maximum_worst_drawdown:.2%}"
        )
    if research.selected_positive_folds < criteria.minimum_positive_folds:
        failures.append(
            f"盈利窗口{research.selected_positive_folds}/{len(research.folds)}少于"
            f"{criteria.minimum_positive_folds}个"
        )
    if minimum_fold_trades < criteria.minimum_trades_per_fold:
        failures.append(
            f"单折最少交易{minimum_fold_trades}笔少于{criteria.minimum_trades_per_fold}笔"
        )

    fold_details = []
    for fold in research.folds:
        selection = fold.research
        result = selection.selected_test_result
        fold_details.append(
            {
                "fold": fold.fold,
                "test_start": fold.test_start.isoformat(),
                "test_end": fold.test_end.isoformat(),
                "return": result.total_return,
                "max_drawdown": result.max_drawdown,
                "trades": result.trade_count,
                "parameters": {
                    "entry_mode": selection.entry_mode,
                    "pullback_lookback": selection.pullback_lookback,
                    "atr_multiple": selection.atr_multiple,
                    "direction": selection.direction,
                    "min_trend_strength": selection.min_trend_strength,
                    "min_adx": selection.min_adx,
                    "higher_timeframe_filter": selection.use_higher_timeframe_filter,
                    "higher_timeframe_mode": getattr(
                        selection, "higher_timeframe_mode", "standard"
                    ),
                    "trend_slope_lookback": selection.trend_slope_lookback,
                    "cooldown_bars": selection.cooldown_bars,
                },
            }
        )
    criteria_dict = asdict(criteria)
    details: dict[str, object] = {
        "baseline_compounded_return": research.baseline_compounded_return,
        "selected_compounded_return": research.selected_compounded_return,
        "selected_worst_drawdown": research.selected_worst_drawdown,
        "positive_folds": research.selected_positive_folds,
        "stable_parameter_set": research.stable_parameter_set,
        "folds": fold_details,
    }
    hash_payload = {
        "strategy_name": strategy_name,
        "strategy_version": strategy_version,
        "symbol": research.symbol,
        "interval": research.interval,
        "criteria": criteria_dict,
        "details": details,
    }
    # 调用稳定JSON序列化后计算SHA-256，相同评估内容重跑会得到同一个幂等键。
    serialized = json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evaluation_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    approved = not failures
    reason = "通过全部样本外准入门槛" if approved else "；".join(failures)
    return QualificationDecision(
        evaluation_hash=evaluation_hash,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        symbol=research.symbol,
        interval=research.interval,
        approved=approved,
        compounded_return=research.selected_compounded_return,
        worst_drawdown=research.selected_worst_drawdown,
        positive_folds=research.selected_positive_folds,
        total_folds=len(research.folds),
        minimum_fold_trades=minimum_fold_trades,
        criteria=criteria_dict,
        details=details,
        reason=reason,
        evaluated_at=evaluated_at,
    )
