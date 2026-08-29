"""EMA vectorbt回测模块测试。"""

import numpy as np
import pandas as pd
import pytest

import gold_crypto_quant.backtest.ema_vectorbt as ema_vectorbt
from gold_crypto_quant.backtest import EmaBacktestConfig, run_ema_backtest
from gold_crypto_quant.backtest.ema_vectorbt import (
    _detect_daily_fuses,
    _detect_drawdown_fuse,
)
from gold_crypto_quant.strategy import EmaTrendParameters


def make_bars(count: int = 300) -> pd.DataFrame:
    """创建带有上涨、下跌和再次上涨阶段的确定性测试K线。"""
    # 生成三个趋势阶段，使快慢EMA至少发生一次方向变化。
    close = np.concatenate(
        [
            np.linspace(100.0, 130.0, count // 3),
            np.linspace(130.0, 80.0, count // 3),
            np.linspace(80.0, 140.0, count - 2 * (count // 3)),
        ]
    )
    # 使用标准分钟频率构造UTC索引，保证和MySQL读取结果的时区格式一致。
    index = pd.date_range("2025-01-01", periods=count, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 10.0,
        },
        index=index,
    )


def test_run_ema_backtest_returns_finite_summary() -> None:
    """正常K线应生成有限数值的回测摘要。"""
    # 缩短EMA周期，减少单元测试所需预热数据量。
    strategy = EmaTrendParameters(fast_period=5, slow_period=10, trend_period=20)
    # 调用回测主方法，验证完整信号、仓位、成本和绩效计算链路。
    result, portfolio = run_ema_backtest(
        make_bars(),
        symbol="BTC_USDT",
        interval="5m",
        strategy=strategy,
        config=EmaBacktestConfig(),
    )

    assert result.symbol == "BTC_USDT"
    assert result.bar_count == 300
    assert result.trade_count == portfolio.trades.count()
    assert np.isfinite(result.total_pnl)
    assert np.isfinite(result.max_drawdown)


def test_run_ema_backtest_rejects_unknown_interval() -> None:
    """未支持的周期必须提前报错，避免频率被错误解释。"""
    with pytest.raises(ValueError, match="unsupported backtest interval"):
        # 调用回测入口并传入不受支持的2分钟周期。
        run_ema_backtest(
            make_bars(),
            symbol="BTC_USDT",
            interval="2m",
            strategy=EmaTrendParameters(fast_period=5, slow_period=10, trend_period=20),
        )


def test_run_ema_backtest_requires_enough_bars() -> None:
    """K线数量不足EMA200预热时必须拒绝回测。"""
    with pytest.raises(ValueError, match="not enough bars"):
        # 默认策略需要超过200根K线，这里只传入100根以验证保护逻辑。
        run_ema_backtest(make_bars(100), symbol="ETH_USDT", interval="5m")


def test_atr_take_profit_closes_trade_at_intrabar_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用ATR止盈后，最高价触及目标的K线应完成平仓。"""
    bars = make_bars()
    bars.loc[:, ["open", "high", "low", "close"]] = [100.0, 101.0, 99.0, 100.0]
    bars.loc[bars.index[202], "high"] = 110.0
    signals = pd.DataFrame(
        False,
        index=bars.index,
        columns=["long_entry", "long_exit", "short_entry", "short_exit"],
    )
    signals["close"] = 100.0
    signals["ema_fast"] = 101.0
    signals["ema_slow"] = 100.0
    signals["ema_trend"] = 99.0
    signals.loc[bars.index[201], "long_entry"] = True
    # 替换信号生成方法，使测试只验证vectorbt的ATR止盈执行。
    monkeypatch.setattr(ema_vectorbt, "generate_ema_signals", lambda _bars, _strategy: signals)

    # 调用正式回测；进场ATR约2，2ATR止盈目标约104，应在下一根最高价触及时退出。
    result, portfolio = run_ema_backtest(
        bars,
        symbol="BTC_USDT",
        interval="5m",
        config=EmaBacktestConfig(take_profit_atr_multiple=2.0),
    )

    trade = portfolio.trades.records_readable.iloc[0]
    assert result.trade_count == 1
    assert trade["Exit Timestamp"] == bars.index[202]
    assert result.total_return > 0


def test_trailing_stop_rejects_cooldown_combination() -> None:
    """移动止损无法按原固定止损识别冷却起点时必须失败关闭。"""
    with pytest.raises(ValueError, match="cannot be combined"):
        # 调用正式回测并传入不兼容组合，防止研究结果错误套用固定止损冷却逻辑。
        run_ema_backtest(
            make_bars(),
            symbol="BTC_USDT",
            interval="5m",
            strategy=EmaTrendParameters(
                fast_period=5,
                slow_period=10,
                trend_period=20,
                cooldown_bars=5,
            ),
            config=EmaBacktestConfig(trailing_stop=True),
        )


def test_detect_daily_fuse_uses_previous_day_close_as_baseline() -> None:
    """每日亏损应按UTC日界线和前一日收盘权益计算。"""
    index = pd.to_datetime(["2025-01-01 23:55:00Z", "2025-01-02 00:00:00Z", "2025-01-02 00:05:00Z"])
    equity = pd.Series([10_000.0, 9_900.0, 9_790.0], index=index)

    # 调用每日熔断检测；第二日相对前收跌2.1%，应在第三根K线确认触发。
    breaches = _detect_daily_fuses(equity, EmaBacktestConfig())

    assert breaches == [(pd.Timestamp("2025-01-02", tz="UTC"), 2)]


def test_detect_drawdown_fuse_returns_first_breach() -> None:
    """总回撤检测必须返回首次达到8%的位置。"""
    index = pd.date_range("2025-01-01", periods=4, freq="5min", tz="UTC")
    equity = pd.Series([10_000.0, 10_200.0, 9_500.0, 9_300.0], index=index)

    # 调用总回撤检测；最后一根相对峰值回撤超过8%。
    breach_position = _detect_drawdown_fuse(equity, EmaBacktestConfig())

    assert breach_position == 3


def test_daily_fuse_blocks_later_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """日熔断触发后，同一UTC日期内的后续信号不得再次开仓。"""
    bars = make_bars()
    bars.loc[:, ["open", "high", "low", "close"]] = [100.0, 101.0, 99.0, 100.0]
    # 第一次开仓后的下一根K线下探止损价，制造超过2%的单笔测试亏损。
    bars.loc[bars.index[202], "low"] = 96.0
    signals = pd.DataFrame(
        False,
        index=bars.index,
        columns=["long_entry", "long_exit", "short_entry", "short_exit"],
    )
    signals["close"] = 100.0
    signals["ema_fast"] = 101.0
    signals["ema_slow"] = 100.0
    signals["ema_trend"] = 99.0
    signals.loc[bars.index[201], "long_entry"] = True
    signals.loc[bars.index[204], "long_entry"] = True
    # 替换策略信号生成方法，使测试只验证熔断层，不受EMA交叉时点影响。
    monkeypatch.setattr(ema_vectorbt, "generate_ema_signals", lambda _bars, _strategy: signals)

    # 调用完整回测入口；风险提高到2%是为了用一次确定性止损触发日熔断。
    result, portfolio = run_ema_backtest(
        bars,
        symbol="BTC_USDT",
        interval="5m",
        config=EmaBacktestConfig(risk_per_trade=0.02),
    )

    assert result.daily_fuse_count == 1
    assert result.trade_count == 1
    assert portfolio.orders.count() == 2


def test_trend_strength_filter_uses_previous_closed_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """执行K线自身的EMA变化不得被用于决定该根开盘是否进场。"""
    bars = make_bars()
    signals = pd.DataFrame(
        False,
        index=bars.index,
        columns=["long_entry", "long_exit", "short_entry", "short_exit"],
    )
    signals["close"] = bars["close"]
    signals["ema_fast"] = 100.0
    signals["ema_slow"] = 100.0
    signals["ema_trend"] = 99.0
    entry_position = 201
    signals.loc[bars.index[entry_position], "long_entry"] = True
    # 只有执行K线自己的EMA差很大，上一根已收盘K线的EMA差仍为0。
    signals.loc[bars.index[entry_position], "ema_fast"] = 120.0
    # 替换信号生成调用，使测试精确控制信号与指标出现的时点。
    monkeypatch.setattr(ema_vectorbt, "generate_ema_signals", lambda _bars, _strategy: signals)

    # 调用回测并设置正数强度门槛；正确使用shift(1)时这笔交易必须被拒绝。
    result, _portfolio = run_ema_backtest(
        bars,
        symbol="BTC_USDT",
        interval="5m",
        strategy=EmaTrendParameters(min_trend_strength=0.5),
    )

    assert result.trade_count == 0


def test_atr_stop_cooldown_blocks_nearby_reentry(monkeypatch: pytest.MonkeyPatch) -> None:
    """ATR止损后的冷却K线内不得执行新的开仓信号。"""
    bars = make_bars()
    bars.loc[:, ["open", "high", "low", "close"]] = [100.0, 101.0, 99.0, 100.0]
    bars.loc[bars.index[202], "low"] = 96.0
    signals = pd.DataFrame(
        False,
        index=bars.index,
        columns=["long_entry", "long_exit", "short_entry", "short_exit"],
    )
    signals["close"] = 100.0
    signals["ema_fast"] = 101.0
    signals["ema_slow"] = 100.0
    signals["ema_trend"] = 99.0
    signals.loc[bars.index[201], "long_entry"] = True
    signals.loc[bars.index[204], "long_entry"] = True
    # 替换信号生成方法，第一笔止损后第二个信号位于5根冷却K线之内。
    monkeypatch.setattr(ema_vectorbt, "generate_ema_signals", lambda _bars, _strategy: signals)

    # 调用完整回测入口，验证冷却遮罩与vectorbt止损和熔断流程共同生效。
    result, portfolio = run_ema_backtest(
        bars,
        symbol="BTC_USDT",
        interval="5m",
        strategy=EmaTrendParameters(cooldown_bars=5),
    )

    assert result.trade_count == 1
    assert portfolio.orders.count() == 2
