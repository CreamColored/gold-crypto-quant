"""把已批准样本外研究结果解析为唯一可部署策略参数。"""

from dataclasses import dataclass

from gold_crypto_quant.storage.strategy_signals import QualificationContext
from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters

DIRECTION_FLAGS = {
    "both": (True, True),
    "long": (True, False),
    "short": (False, True),
}


@dataclass(frozen=True, slots=True)
class ApprovedDeploymentParameters:
    """通过准入且所有滚动窗口一致的实时策略参数。"""

    strategy: EmaTrendParameters
    atr_multiple: float


def resolve_approved_parameters(
    context: QualificationContext,
) -> ApprovedDeploymentParameters:
    """只接受APPROVED且各滚动窗口选择完全一致的参数集合。"""
    if context.decision != "APPROVED":
        raise PermissionError("strategy qualification is not approved")
    details = context.details
    if not isinstance(details, dict):
        raise PermissionError("approved qualification details are missing")
    if details.get("stable_parameter_set") is not True:
        raise PermissionError("approved qualification parameters are not stable across folds")
    folds = details.get("folds")
    if not isinstance(folds, list) or not folds:
        raise PermissionError("approved qualification fold parameters are missing")
    parameter_sets: list[dict[str, object]] = []
    for fold in folds:
        if not isinstance(fold, dict) or not isinstance(fold.get("parameters"), dict):
            raise PermissionError("approved qualification contains invalid fold parameters")
        parameter_sets.append(fold["parameters"])
    first = parameter_sets[0]
    if any(parameters != first for parameters in parameter_sets[1:]):
        raise PermissionError("approved qualification fold parameters do not match")

    direction = str(first.get("direction"))
    if direction not in DIRECTION_FLAGS:
        raise PermissionError("approved qualification direction is invalid")
    allow_long, allow_short = DIRECTION_FLAGS[direction]
    try:
        strategy = EmaTrendParameters(
            min_trend_strength=float(first["min_trend_strength"]),
            allow_long=allow_long,
            allow_short=allow_short,
            min_adx=float(first["min_adx"]),
            use_higher_timeframe_filter=bool(first["higher_timeframe_filter"]),
            # 旧版批准记录没有该字段时保持0，确保历史审计记录仍可安全解析。
            trend_slope_lookback=int(first.get("trend_slope_lookback", 0)),
            cooldown_bars=int(first["cooldown_bars"]),
            entry_mode=str(first["entry_mode"]),
            pullback_lookback=int(first["pullback_lookback"]),
        )
        atr_multiple = float(first["atr_multiple"])
    except (KeyError, TypeError, ValueError) as error:
        raise PermissionError("approved qualification parameters cannot be parsed") from error
    if atr_multiple <= 0:
        raise PermissionError("approved ATR multiple must be positive")
    if strategy.cooldown_bars > 0:
        raise PermissionError("approved cooldown requires completed fill and stop recovery support")
    return ApprovedDeploymentParameters(strategy=strategy, atr_multiple=atr_multiple)
