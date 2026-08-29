"""EMA策略按时间隔离的训练集/验证集参数研究。"""

from dataclasses import dataclass, replace
from itertools import product

import pandas as pd

from gold_crypto_quant.backtest.ema_vectorbt import (
    EmaBacktestConfig,
    EmaBacktestResult,
    run_ema_backtest,
)
from gold_crypto_quant.strategy.ema_trend import EmaTrendParameters

DIRECTION_FLAGS = {
    "both": (True, True),
    "long": (True, False),
    "short": (False, True),
}


class InsufficientResearchData(RuntimeError):
    """训练区间没有候选达到最低交易笔数。"""


@dataclass(frozen=True, slots=True)
class HoldoutResearchResult:
    """训练集选择结果和未参与选择的样本外验证结果。"""

    symbol: str
    interval: str
    train_bar_count: int
    test_bar_count: int
    candidate_count: int
    min_trend_strength: float
    atr_multiple: float
    direction: str
    min_adx: float
    use_higher_timeframe_filter: bool
    trend_slope_lookback: int
    cooldown_bars: int
    entry_mode: str
    pullback_lookback: int
    train_score: float
    train_result: EmaBacktestResult
    baseline_test_result: EmaBacktestResult
    selected_test_result: EmaBacktestResult


@dataclass(frozen=True, slots=True)
class RollingFoldResult:
    """一个扩展训练窗口及其紧随其后的独立验证结果。"""

    fold: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    research: HoldoutResearchResult


@dataclass(frozen=True, slots=True)
class RollingResearchResult:
    """多个互不重叠样本外区间的汇总结果。"""

    symbol: str
    interval: str
    folds: tuple[RollingFoldResult, ...]
    baseline_compounded_return: float
    selected_compounded_return: float
    baseline_worst_drawdown: float
    selected_worst_drawdown: float
    baseline_positive_folds: int
    selected_positive_folds: int
    stable_parameter_set: bool


def _research_score(result: EmaBacktestResult, minimum_trades: int) -> float:
    """用收益减半倍回撤评价训练集，并拒绝交易数过少的候选。"""
    if result.trade_count < minimum_trades:
        return float("-inf")
    return result.total_return - 0.5 * result.max_drawdown


def _build_rolling_boundaries(
    bar_count: int,
    *,
    fold_count: int,
    test_fraction: float,
) -> list[tuple[int, int]]:
    """返回每折训练结束位置和验证结束位置。"""
    if fold_count < 2:
        raise ValueError("fold_count must be at least 2")
    if not 0 < test_fraction < 1 / fold_count:
        raise ValueError("test_fraction leaves no initial training segment")
    test_size = int(bar_count * test_fraction)
    if test_size < 1:
        raise ValueError("not enough bars for rolling test windows")
    initial_train_size = bar_count - fold_count * test_size
    boundaries: list[tuple[int, int]] = []
    for fold_index in range(fold_count):
        train_end = initial_train_size + fold_index * test_size
        test_end = bar_count if fold_index == fold_count - 1 else train_end + test_size
        boundaries.append((train_end, test_end))
    return boundaries


def _compound_returns(returns: list[float]) -> float:
    """把多个连续样本外区间收益按资金连续滚动方式复合。"""
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
    return compounded - 1.0


def run_holdout_research(
    bars: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    train_fraction: float = 0.7,
    trend_strengths: tuple[float, ...] = (0.0, 0.1, 0.15, 0.2),
    atr_multiples: tuple[float, ...] = (1.5, 2.0, 2.5),
    directions: tuple[str, ...] = ("both", "long", "short"),
    adx_thresholds: tuple[float, ...] = (0.0,),
    higher_timeframe_filters: tuple[bool, ...] = (False,),
    trend_slope_lookbacks: tuple[int, ...] = (0,),
    cooldown_options: tuple[int, ...] = (0,),
    entry_variants: tuple[tuple[str, int], ...] = (("cross", 3),),
    minimum_train_trades: int = 8,
    base_strategy: EmaTrendParameters | None = None,
    base_config: EmaBacktestConfig | None = None,
) -> HoldoutResearchResult:
    """只用前段数据选参，再在后段数据上与原始策略进行一次对照。"""
    if not 0.5 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be in [0.5, 0.9]")
    if minimum_train_trades < 1:
        raise ValueError("minimum_train_trades must be positive")
    invalid_directions = set(directions) - DIRECTION_FLAGS.keys()
    if invalid_directions:
        raise ValueError(f"unsupported research directions: {sorted(invalid_directions)}")

    base_strategy = base_strategy or EmaTrendParameters()
    base_config = base_config or EmaBacktestConfig()
    split_position = int(len(bars) * train_fraction)
    minimum_segment_size = base_strategy.trend_period + base_config.atr_period + 2
    if split_position < minimum_segment_size or len(bars) - split_position < minimum_segment_size:
        raise ValueError("not enough bars for isolated train and test segments")

    train_bars = bars.iloc[:split_position]
    test_start = bars.index[split_position]
    # 验证集前附带预热K线，但trade_start会禁止预热区间开仓，防止训练交易混入验证结果。
    warmup_start = max(0, split_position - minimum_segment_size)
    test_bars = bars.iloc[warmup_start:]

    best_score = float("-inf")
    best_strategy: EmaTrendParameters | None = None
    best_config: EmaBacktestConfig | None = None
    best_direction = ""
    best_train_result: EmaBacktestResult | None = None

    candidates = list(
        product(
            trend_strengths,
            atr_multiples,
            directions,
            adx_thresholds,
            higher_timeframe_filters,
            trend_slope_lookbacks,
            cooldown_options,
            entry_variants,
        )
    )
    for (
        trend_strength,
        atr_multiple,
        direction,
        min_adx,
        higher_filter,
        trend_slope_lookback,
        cooldown_bars,
        entry_variant,
    ) in candidates:
        entry_mode, pullback_lookback = entry_variant
        allow_long, allow_short = DIRECTION_FLAGS[direction]
        candidate_strategy = replace(
            base_strategy,
            min_trend_strength=trend_strength,
            allow_long=allow_long,
            allow_short=allow_short,
            min_adx=min_adx,
            use_higher_timeframe_filter=higher_filter,
            trend_slope_lookback=trend_slope_lookback,
            cooldown_bars=cooldown_bars,
            entry_mode=entry_mode,
            pullback_lookback=pullback_lookback,
        )
        candidate_config = replace(base_config, atr_multiple=atr_multiple)
        # 调用正式回测方法评估训练集候选，成本、止损和熔断规则全部保持生效。
        train_result, _portfolio = run_ema_backtest(
            train_bars,
            symbol=symbol,
            interval=interval,
            strategy=candidate_strategy,
            config=candidate_config,
        )
        score = _research_score(train_result, minimum_train_trades)
        if score > best_score:
            best_score = score
            best_strategy = candidate_strategy
            best_config = candidate_config
            best_direction = direction
            best_train_result = train_result

    if best_strategy is None or best_config is None or best_train_result is None:
        raise InsufficientResearchData(
            f"no candidate reached {minimum_train_trades} training trades for {symbol} {interval}"
        )

    # 调用原始参数回测验证集，作为判断新过滤是否真正改善样本外结果的基线。
    baseline_test_result, _baseline_portfolio = run_ema_backtest(
        test_bars,
        symbol=symbol,
        interval=interval,
        strategy=base_strategy,
        config=base_config,
        trade_start=test_start,
    )
    # 只把训练集选出的唯一最佳参数应用到验证集，不允许查看结果后再次调参。
    selected_test_result, _selected_portfolio = run_ema_backtest(
        test_bars,
        symbol=symbol,
        interval=interval,
        strategy=best_strategy,
        config=best_config,
        trade_start=test_start,
    )
    return HoldoutResearchResult(
        symbol=symbol,
        interval=interval,
        train_bar_count=len(train_bars),
        test_bar_count=len(bars) - split_position,
        candidate_count=len(candidates),
        min_trend_strength=best_strategy.min_trend_strength,
        atr_multiple=best_config.atr_multiple,
        direction=best_direction,
        min_adx=best_strategy.min_adx,
        use_higher_timeframe_filter=best_strategy.use_higher_timeframe_filter,
        trend_slope_lookback=best_strategy.trend_slope_lookback,
        cooldown_bars=best_strategy.cooldown_bars,
        entry_mode=best_strategy.entry_mode,
        pullback_lookback=best_strategy.pullback_lookback,
        train_score=best_score,
        train_result=best_train_result,
        baseline_test_result=baseline_test_result,
        selected_test_result=selected_test_result,
    )


def run_rolling_research(
    bars: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    fold_count: int = 3,
    test_fraction: float = 0.2,
    trend_strengths: tuple[float, ...] = (0.0, 0.1, 0.15, 0.2),
    atr_multiples: tuple[float, ...] = (1.5, 2.0, 2.5),
    directions: tuple[str, ...] = ("both", "long", "short"),
    adx_thresholds: tuple[float, ...] = (0.0,),
    higher_timeframe_filters: tuple[bool, ...] = (False,),
    trend_slope_lookbacks: tuple[int, ...] = (0,),
    cooldown_options: tuple[int, ...] = (0,),
    entry_variants: tuple[tuple[str, int], ...] = (("cross", 3),),
    minimum_train_trades: int = 8,
    base_strategy: EmaTrendParameters | None = None,
    base_config: EmaBacktestConfig | None = None,
) -> RollingResearchResult:
    """在扩展训练窗口上反复选参，并汇总三个独立样本外区间。"""
    base_strategy = base_strategy or EmaTrendParameters()
    base_config = base_config or EmaBacktestConfig()
    # 调用边界构造方法，确保三个验证区间按时间排列且彼此不重叠。
    boundaries = _build_rolling_boundaries(
        len(bars),
        fold_count=fold_count,
        test_fraction=test_fraction,
    )
    minimum_segment_size = base_strategy.trend_period + base_config.atr_period + 2
    if boundaries[0][0] < minimum_segment_size:
        raise ValueError("initial rolling training segment is too short")

    fold_results: list[RollingFoldResult] = []
    baseline_returns: list[float] = []
    selected_returns: list[float] = []
    parameter_sets: list[tuple[float, float, str, float, bool, int, int, str, int]] = []
    previous_test_end = boundaries[0][0]
    for fold_number, (train_end, test_end) in enumerate(boundaries, start=1):
        prefix = bars.iloc[:test_end]
        train_fraction = train_end / test_end
        # 每折调用独立留出研究，只允许使用当前验证区间之前的数据选择参数。
        research = run_holdout_research(
            prefix,
            symbol=symbol,
            interval=interval,
            train_fraction=train_fraction,
            trend_strengths=trend_strengths,
            atr_multiples=atr_multiples,
            directions=directions,
            adx_thresholds=adx_thresholds,
            higher_timeframe_filters=higher_timeframe_filters,
            trend_slope_lookbacks=trend_slope_lookbacks,
            cooldown_options=cooldown_options,
            entry_variants=entry_variants,
            minimum_train_trades=minimum_train_trades,
            base_strategy=base_strategy,
            base_config=base_config,
        )
        fold_results.append(
            RollingFoldResult(
                fold=fold_number,
                train_start=bars.index[0],
                train_end=bars.index[train_end - 1],
                test_start=bars.index[train_end],
                test_end=bars.index[test_end - 1],
                research=research,
            )
        )
        baseline_returns.append(research.baseline_test_result.total_return)
        selected_returns.append(research.selected_test_result.total_return)
        parameter_sets.append(
            (
                research.min_trend_strength,
                research.atr_multiple,
                research.direction,
                research.min_adx,
                research.use_higher_timeframe_filter,
                research.trend_slope_lookback,
                research.cooldown_bars,
                research.entry_mode,
                research.pullback_lookback,
            )
        )
        if train_end != previous_test_end:
            raise RuntimeError("rolling test windows are not contiguous")
        previous_test_end = test_end

    baseline_drawdowns = [fold.research.baseline_test_result.max_drawdown for fold in fold_results]
    selected_drawdowns = [fold.research.selected_test_result.max_drawdown for fold in fold_results]
    return RollingResearchResult(
        symbol=symbol,
        interval=interval,
        folds=tuple(fold_results),
        baseline_compounded_return=_compound_returns(baseline_returns),
        selected_compounded_return=_compound_returns(selected_returns),
        baseline_worst_drawdown=max(baseline_drawdowns),
        selected_worst_drawdown=max(selected_drawdowns),
        baseline_positive_folds=sum(value > 0 for value in baseline_returns),
        selected_positive_folds=sum(value > 0 for value in selected_returns),
        stable_parameter_set=len(set(parameter_sets)) == 1,
    )
