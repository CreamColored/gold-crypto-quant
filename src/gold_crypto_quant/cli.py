"""项目命令行入口。

这里仅提供无交易风险的管理命令，不包含下单功能。
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from gold_crypto_quant.backtest import (
    EmaBacktestConfig,
    InsufficientResearchData,
    diagnose_trades,
    run_ema_backtest,
    run_holdout_research,
    run_rolling_research,
)
from gold_crypto_quant.config import Settings, get_settings
from gold_crypto_quant.exchanges.gate import GateTestnetClient
from gold_crypto_quant.exchanges.oanda import (
    OandaApiError,
    OandaPracticeAccount,
    OandaPracticeClient,
    resolve_oanda_account_id,
)
from gold_crypto_quant.market_data import import_gate_history, import_oanda_history
from gold_crypto_quant.market_data.gate_history import GATE_TESTNET_VENUE
from gold_crypto_quant.market_data.oanda_history import OANDA_PRACTICE_VENUE
from gold_crypto_quant.risk.qualification import evaluate_rolling_research
from gold_crypto_quant.runtime import (
    MARKET_DATA_SERVICE_NAME,
    OANDA_MARKET_DATA_SERVICE_NAME,
    MarketDataRunner,
    RunnerConfig,
    SingleInstanceLock,
    install_shutdown_signal_handlers,
    run_paper_signal_cycle,
)
from gold_crypto_quant.runtime.oanda_paper_entry import execute_approved_oanda_paper_entry
from gold_crypto_quant.runtime.paper_entry import execute_approved_paper_entry
from gold_crypto_quant.storage import (
    check_connection,
    create_schema,
    sync_schema_comments,
)
from gold_crypto_quant.storage.execution_status import read_execution_safety_status
from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.market_health import refresh_market_health
from gold_crypto_quant.storage.paper_account import (
    OANDA_PAPER_ACCOUNT_VENUE,
    refresh_paper_account,
)
from gold_crypto_quant.storage.paper_fills import (
    close_position_for_ema_signal,
    monitor_paper_positions,
)
from gold_crypto_quant.storage.paper_simulation import (
    read_paper_simulation_state,
    refresh_paper_simulation,
)
from gold_crypto_quant.storage.qualifications import save_qualification
from gold_crypto_quant.storage.runtime_risk import (
    evaluate_and_save_runtime_risk,
    save_account_snapshot,
)
from gold_crypto_quant.storage.service_state import read_service_state
from gold_crypto_quant.storage.system_readiness import read_system_readiness
from gold_crypto_quant.strategy import EmaTrendParameters


def _venue_for_symbol(symbol: str) -> str:
    """根据第一阶段唯一品种代码选择测试交易场所。"""
    return OANDA_PRACTICE_VENUE if symbol == "XAU_USD" else GATE_TESTNET_VENUE


def _read_oanda_practice_account(
    settings: Settings,
) -> tuple[OandaPracticeAccount, bool]:
    """只读返回已验证的Practice账户摘要及是否自动选择账户。"""
    with OandaPracticeClient.from_settings(settings) as client:
        # 调用授权账户解析，防止配置编号与当前Token不匹配。
        account_id, auto_selected = resolve_oanda_account_id(
            client, settings.oanda_practice_account_id
        )
        # 调用只读摘要接口；返回后with负责关闭底层连接池。
        account = client.get_account_summary(account_id)
    return account, auto_selected


def main() -> None:
    """解析命令行参数，并分发到状态、连接检查或建表功能。"""
    # 创建参数解析器，让同一个入口支持日常状态检查和数据库维护命令。
    parser = argparse.ArgumentParser(description="Gold Crypto Quant")
    # command 可以省略；省略时只显示状态，避免误触发数据库操作。
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "status",
            "db-check",
            "init-db",
            "sync-comments",
            "import-gate-bars",
            "import-oanda-bars",
            "backtest-ema",
            "diagnose-ema",
            "research-ema",
            "walkforward-ema",
            "walkforward-filters",
            "walkforward-pullback",
            "walkforward-regime",
            "walkforward-ema12",
            "qualify-ema",
            "execution-safety",
            "system-readiness",
            "risk-snapshot",
            "market-health",
            "market-runner",
            "market-runner-status",
            "oanda-runner",
            "oanda-runner-status",
            "oanda-account-check",
            "oanda-risk-snapshot",
            "paper-signal-cycle",
            "paper-simulation-status",
            "oanda-paper-simulation-status",
        ),
        default="status",
    )
    # K线导入参数只在 import-gate-bars 命令中使用，其他命令会安全忽略这些默认值。
    parser.add_argument("--limit", type=int, default=1000, help="每个合约周期获取的K线数量")
    # history-days用于分批向前回溯；省略时保持原来的“仅刷新最近K线”行为。
    parser.add_argument("--history-days", type=int, help="向前补齐指定天数的历史K线")
    parser.add_argument(
        "--contracts",
        nargs="+",
        default=["BTC_USDT", "ETH_USDT"],
        help="研究品种代码；黄金使用XAU_USD",
    )
    parser.add_argument(
        "--intervals", nargs="+", default=["5m", "15m", "30m", "1h"], help="K线周期"
    )
    parser.add_argument(
        "--oanda-instrument",
        default="XAU_USD",
        help="OANDA Practice黄金品种代码",
    )
    parser.add_argument(
        "--oanda-paper-interval",
        default="30m",
        choices=("5m", "15m", "30m", "1h"),
        help="允许黄金本地模拟开仓的唯一主周期；其他周期只记录影子信号",
    )
    # 以下调度参数只供market-runner使用，默认每60秒更新一次最近100根K线。
    parser.add_argument("--poll-seconds", type=float, default=60.0, help="行情轮询间隔秒数")
    parser.add_argument("--retry-initial-seconds", type=float, default=5.0, help="首次重试等待秒数")
    parser.add_argument("--retry-max-seconds", type=float, default=300.0, help="最大重试等待秒数")
    parser.add_argument("--max-cycles", type=int, help="成功运行指定轮数后退出，仅用于验证")
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path(".runtime/market-data-runner.lock"),
        help="防止行情服务重复启动的本地锁文件",
    )
    parser.add_argument(
        "--oanda-lock-file",
        type=Path,
        default=Path(".runtime/oanda-market-data-runner.lock"),
        help="防止OANDA行情服务重复启动的本地锁文件",
    )
    parser.add_argument("--bar-limit", type=int, default=500, help="实时信号使用的最近K线数量")
    parser.add_argument(
        "--enable-signals",
        action="store_true",
        help="在长期行情服务每轮成功后运行EMA信号观察，仍不创建订单",
    )
    # 初始权益只用于回测资金和风险计算，不会读取或修改测试网账户余额。
    parser.add_argument("--initial-equity", type=float, default=10_000.0, help="回测初始权益")
    # 解析 PyCharm 运行参数或终端传入的参数，结果保存在 args.command。
    args = parser.parse_args()

    # 统一通过配置工厂读取 .env；调用方不直接接触账号、密码等敏感信息。
    settings = get_settings()
    if args.command == "db-check":
        # check_connection 内部执行 SELECT 1，只验证连接，不修改任何业务数据。
        print(f"Database connected: {check_connection()}")
        return
    if args.command == "init-db":
        # create_schema 采用“缺表才创建”的方式，可安全重复执行，不会删除已有表。
        create_schema()
        print("Database schema initialized")
        return
    if args.command == "sync-comments":
        # 已有表不会被 create_all 更新；该命令专门同步模型中的表注释和字段注释。
        sync_schema_comments()
        print("Database comments synchronized")
        return
    if args.command == "import-gate-bars":
        # with 会在导入结束或异常时关闭 Gate 网络连接，避免连接池资源泄漏。
        with GateTestnetClient.from_settings(settings) as client:
            results = import_gate_history(
                client,
                contracts=tuple(args.contracts),
                intervals=tuple(args.intervals),
                limit=args.limit,
                history_days=args.history_days,
            )
        # 输出每个批次的数量，不打印API密钥、账户余额或其他敏感信息。
        for result in results:
            history_status = "受测试网10000点限制" if result.history_limited else "达到请求范围"
            print(
                f"{result.contract} {result.interval}: fetched={result.fetched}, "
                f"stored={result.stored}, skipped_open={result.skipped_open}, "
                f"pages={result.pages}, history={history_status}"
            )
        return
    if args.command == "import-oanda-bars":
        # with结束时调用Practice客户端close；客户端没有任何订单或账户修改方法。
        with OandaPracticeClient.from_settings(settings) as client:
            # 调用黄金历史导入，按品种、周期和开盘时间幂等写入MySQL。
            results = import_oanda_history(
                client,
                instrument=args.oanda_instrument,
                intervals=tuple(args.intervals),
                limit=args.limit,
                history_days=args.history_days,
            )
        for result in results:
            print(
                f"{result.instrument} {result.interval}: fetched={result.fetched}, "
                f"stored={result.stored}, skipped_open={result.skipped_open}, "
                f"pages={result.pages}"
            )
        print("OANDA environment: Practice")
        print("Exchange order submission available: False")
        return
    if args.command == "backtest-ema":
        # 创建统一回测参数；125倍是名义仓位硬上限，实际仓位仍由单笔风险决定。
        backtest_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 从MySQL只读取已经收盘且按时间升序排列的K线。
                bars = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                # 运行EMA 20/50/200多空回测；该调用只在内存中计算，不连接交易接口。
                result, _portfolio = run_ema_backtest(
                    bars,
                    symbol=contract,
                    interval=interval,
                    config=backtest_config,
                )
                drawdown_status = "通过" if result.passed_drawdown_limit else "超限"
                permanent_fuse = "已触发" if result.drawdown_fuse_triggered else "未触发"
                print(
                    f"{result.symbol} {result.interval}: bars={result.bar_count}, "
                    f"trades={result.trade_count}, pnl={result.total_pnl:.2f}, "
                    f"return={result.total_return:.2%}, max_dd={result.max_drawdown:.2%}, "
                    f"win_rate={result.win_rate:.2%}, profit_factor={result.profit_factor:.2f}, "
                    f"日熔断={result.daily_fuse_count}次, 8%永久熔断={permanent_fuse}, "
                    f"回撤结果={drawdown_status}"
                )
        return
    if args.command == "diagnose-ema":
        # 诊断使用与正式回测完全相同的初始权益、成本、止损和熔断参数。
        diagnostic_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 从MySQL调用标准行情读取方法，读取前会检查连续性和OHLC合法性。
                bars = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                # 先调用正式回测，确保诊断分析的是风控生效后的真实交易序列。
                _result, portfolio = run_ema_backtest(
                    bars,
                    symbol=contract,
                    interval=interval,
                    config=diagnostic_config,
                )
                # 调用逐笔诊断方法，拆分交易方向、成本、退出类型和进场市场状态。
                diagnostic = diagnose_trades(
                    bars,
                    portfolio,
                    symbol=contract,
                    interval=interval,
                    config=diagnostic_config,
                )
                print(
                    f"{contract} {interval}: trades={diagnostic.trade_count}, "
                    f"net={diagnostic.net_pnl:.2f}, before_fees={diagnostic.pnl_before_fees:.2f}, "
                    f"fees={diagnostic.total_fees:.2f}, "
                    f"estimated_slippage={diagnostic.estimated_slippage:.2f}"
                )
                print(
                    f"  多单={diagnostic.long_count}笔/{diagnostic.long_pnl:.2f}, "
                    f"空单={diagnostic.short_count}笔/{diagnostic.short_pnl:.2f}, "
                    f"ATR止损={diagnostic.atr_stop_count}, "
                    f"开盘退出={diagnostic.open_price_exit_count}, "
                    f"其他退出={diagnostic.other_exit_count}"
                )
                print(
                    f"  平均持仓={diagnostic.average_holding_bars:.1f}根, "
                    f"最长连亏={diagnostic.longest_losing_streak}笔, "
                    f"赢家趋势强度={diagnostic.winning_trend_strength:.3f}, "
                    f"输家趋势强度={diagnostic.losing_trend_strength:.3f}, "
                    f"进场ATR占比={diagnostic.average_entry_atr_percent:.2%}"
                )
        return
    if args.command == "research-ema":
        # 参数研究同样使用正式成本和风控配置，不允许使用无手续费的乐观假设。
        research_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，研究前先执行连续性和OHLC质量检查。
                bars = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                # 调用时间隔离研究：前70%选择参数，后30%仅做一次样本外验证。
                research = run_holdout_research(
                    bars,
                    symbol=contract,
                    interval=interval,
                    base_config=research_config,
                )
                baseline = research.baseline_test_result
                selected = research.selected_test_result
                print(
                    f"{contract} {interval}: candidates={research.candidate_count}, "
                    f"train_bars={research.train_bar_count}, test_bars={research.test_bar_count}"
                )
                print(
                    f"  训练集选择: strength>={research.min_trend_strength:.2f}, "
                    f"ATR={research.atr_multiple:.1f}, direction={research.direction}, "
                    f"return={research.train_result.total_return:.2%}, "
                    f"max_dd={research.train_result.max_drawdown:.2%}, "
                    f"trades={research.train_result.trade_count}"
                )
                print(
                    f"  样本外对照: baseline={baseline.total_return:.2%}/"
                    f"DD {baseline.max_drawdown:.2%}/{baseline.trade_count}笔, "
                    f"selected={selected.total_return:.2%}/"
                    f"DD {selected.max_drawdown:.2%}/{selected.trade_count}笔"
                )
        return
    if args.command == "walkforward-ema":
        # 滚动验证继续沿用正式成本、止损和动态熔断，避免研究环境与运行环境不一致。
        walkforward_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，所有滚动窗口共享同一份已验证历史数据。
                bars = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                # 调用三折扩展窗口研究，每折验证数据都不会参与该折参数选择。
                try:
                    rolling = run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        base_config=walkforward_config,
                    )
                except InsufficientResearchData as exc:
                    # 样本不足属于研究结论而非程序故障；输出原因后继续分析其他品种周期。
                    print(f"{contract} {interval}: 样本不足，{exc}")
                    continue
                print(f"{contract} {interval}: rolling_folds={len(rolling.folds)}")
                for fold in rolling.folds:
                    research = fold.research
                    baseline = research.baseline_test_result
                    selected = research.selected_test_result
                    print(
                        f"  Fold {fold.fold}: strength>={research.min_trend_strength:.2f}, "
                        f"ATR={research.atr_multiple:.1f}, direction={research.direction}, "
                        f"baseline={baseline.total_return:.2%}, "
                        f"selected={selected.total_return:.2%}, "
                        f"selected_DD={selected.max_drawdown:.2%}, "
                        f"trades={selected.trade_count}"
                    )
                stable = "一致" if rolling.stable_parameter_set else "不一致"
                print(
                    f"  样本外汇总: baseline={rolling.baseline_compounded_return:.2%}, "
                    f"selected={rolling.selected_compounded_return:.2%}, "
                    f"最差DD={rolling.selected_worst_drawdown:.2%}, "
                    f"盈利窗口={rolling.selected_positive_folds}/{len(rolling.folds)}, "
                    f"参数稳定性={stable}"
                )
        return
    if args.command == "walkforward-filters":
        # 新过滤器使用受控组合，EMA周期仍固定为20/50/200；多空方向保持完全对称。
        filter_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，保证滚动研究只使用连续且合法的已收盘K线。
                bars = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                try:
                    # 调用三折滚动研究，同时评估ADX、高周期确认、冷却期、方向和ATR。
                    # both、long、short必须同时保留，避免研究阶段先验偏向某一个交易方向。
                    rolling = run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        trend_strengths=(0.0,),
                        atr_multiples=(1.5, 2.0, 2.5),
                        directions=("both", "long", "short"),
                        adx_thresholds=(0.0, 25.0),
                        higher_timeframe_filters=(False, True),
                        cooldown_options=(0, 5),
                        base_config=filter_config,
                    )
                except InsufficientResearchData as exc:
                    # 保持最低8笔训练交易要求，样本不足时跳过而不是放宽标准。
                    print(f"{contract} {interval}: 样本不足，{exc}")
                    continue
                # 从实际研究结果读取候选数，后续增减参数时不会显示过期的硬编码数字。
                candidate_count = rolling.folds[0].research.candidate_count
                print(f"{contract} {interval}: filter_candidates={candidate_count}")
                for fold in rolling.folds:
                    research = fold.research
                    selected = research.selected_test_result
                    higher = "on" if research.use_higher_timeframe_filter else "off"
                    print(
                        f"  Fold {fold.fold}: ADX={research.min_adx:.0f}, HTF={higher}, "
                        f"cooldown={research.cooldown_bars}, ATR={research.atr_multiple:.1f}, "
                        f"direction={research.direction}, return={selected.total_return:.2%}, "
                        f"DD={selected.max_drawdown:.2%}, trades={selected.trade_count}"
                    )
                stable = "一致" if rolling.stable_parameter_set else "不一致"
                print(
                    f"  样本外汇总: baseline={rolling.baseline_compounded_return:.2%}, "
                    f"selected={rolling.selected_compounded_return:.2%}, "
                    f"最差DD={rolling.selected_worst_drawdown:.2%}, "
                    f"盈利窗口={rolling.selected_positive_folds}/{len(rolling.folds)}, "
                    f"参数稳定性={stable}"
                )
        return
    if args.command == "walkforward-pullback":
        # 回踩研究固定其他新过滤器关闭，只比较入场结构、ATR止损和多空方向。
        pullback_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，确保回踩触碰使用的是完整连续的最高价和最低价。
                bars = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                try:
                    # 调用滚动研究，交叉基线只保留一组，回踩分别研究2、3、5根观察窗口。
                    # 同时保留both、long、short，确保BTC做多候选不会被研究入口提前排除。
                    rolling = run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        trend_strengths=(0.0,),
                        atr_multiples=(1.5, 2.0, 2.5),
                        directions=("both", "long", "short"),
                        entry_variants=(
                            ("cross", 3),
                            ("pullback", 2),
                            ("pullback", 3),
                            ("pullback", 5),
                        ),
                        base_config=pullback_config,
                    )
                except InsufficientResearchData as exc:
                    print(f"{contract} {interval}: 样本不足，{exc}")
                    continue
                # 调用研究结果中的候选计数，避免参数网格改变后日志仍显示旧数量。
                candidate_count = rolling.folds[0].research.candidate_count
                print(f"{contract} {interval}: entry_candidates={candidate_count}")
                for fold in rolling.folds:
                    research = fold.research
                    selected = research.selected_test_result
                    print(
                        f"  Fold {fold.fold}: mode={research.entry_mode}, "
                        f"lookback={research.pullback_lookback}, "
                        f"ATR={research.atr_multiple:.1f}, direction={research.direction}, "
                        f"return={selected.total_return:.2%}, "
                        f"DD={selected.max_drawdown:.2%}, trades={selected.trade_count}"
                    )
                stable = "一致" if rolling.stable_parameter_set else "不一致"
                print(
                    f"  样本外汇总: baseline={rolling.baseline_compounded_return:.2%}, "
                    f"selected={rolling.selected_compounded_return:.2%}, "
                    f"最差DD={rolling.selected_worst_drawdown:.2%}, "
                    f"盈利窗口={rolling.selected_positive_folds}/{len(rolling.folds)}, "
                    f"参数稳定性={stable}"
                )
        return
    if args.command == "walkforward-regime":
        # 市场状态研究只增加EMA200斜率开关，并保持20/50/200周期与多空双向不变。
        regime_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，所有判断仅基于按时间排序的已收盘K线。
                bars = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                try:
                    # 调用三折滚动研究；每折只在训练集比较斜率开关和三档ATR距离。
                    # 方向固定both，避免再次通过历史收益选择固定做多或固定做空。
                    rolling = run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        trend_strengths=(0.0,),
                        atr_multiples=(1.5, 2.0, 2.5),
                        directions=("both",),
                        trend_slope_lookbacks=(0, 5, 10),
                        base_config=regime_config,
                    )
                except InsufficientResearchData as exc:
                    # 样本不足时保留拒绝结论，不通过降低最低交易数来制造结果。
                    print(f"{contract} {interval}: 样本不足，{exc}")
                    continue
                candidate_count = rolling.folds[0].research.candidate_count
                print(f"{contract} {interval}: regime_candidates={candidate_count}")
                for fold in rolling.folds:
                    research = fold.research
                    selected = research.selected_test_result
                    print(
                        f"  Fold {fold.fold}: EMA200_slope_lookback="
                        f"{research.trend_slope_lookback}, "
                        f"ATR={research.atr_multiple:.1f}, return={selected.total_return:.2%}, "
                        f"DD={selected.max_drawdown:.2%}, trades={selected.trade_count}"
                    )
                stable = "一致" if rolling.stable_parameter_set else "不一致"
                print(
                    f"  样本外汇总: baseline={rolling.baseline_compounded_return:.2%}, "
                    f"selected={rolling.selected_compounded_return:.2%}, "
                    f"最差DD={rolling.selected_worst_drawdown:.2%}, "
                    f"盈利窗口={rolling.selected_positive_folds}/{len(rolling.folds)}, "
                    f"参数稳定性={stable}"
                )
        return
    if args.command == "walkforward-ema12":
        # 旧系统EMA12规则仅进入历史研究，不写准入表，也不会被纸面交易循环读取。
        ema12_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        ema12_strategy = EmaTrendParameters(
            fast_period=12,
            slow_period=144,
            trend_period=169,
            use_higher_timeframe_filter=True,
            entry_mode="ema12_pullback",
            pullback_lookback=2,
        )
        for contract in args.contracts:
            for interval in args.intervals:
                if interval not in {"15m", "30m"}:
                    print(f"{contract} {interval}: 跳过，EMA12回踩研究仅支持15m和30m")
                    continue
                # 调用标准行情读取方法，所有EMA和回踩判断只使用连续、已收盘的历史K线。
                bars = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                try:
                    # 调用三折滚动研究；只比较ATR止损和1/2/3根回踩观察窗，多空始终同时开启。
                    rolling = run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        trend_strengths=(0.0,),
                        atr_multiples=(1.5, 2.0, 2.5),
                        directions=("both",),
                        higher_timeframe_filters=(True,),
                        entry_variants=(
                            ("ema12_pullback", 1),
                            ("ema12_pullback", 2),
                            ("ema12_pullback", 3),
                        ),
                        base_strategy=ema12_strategy,
                        base_config=ema12_config,
                    )
                except InsufficientResearchData as exc:
                    print(f"{contract} {interval}: 样本不足，{exc}")
                    continue
                candidate_count = rolling.folds[0].research.candidate_count
                print(f"{contract} {interval}: EMA12_candidates={candidate_count}")
                for fold in rolling.folds:
                    research = fold.research
                    selected = research.selected_test_result
                    print(
                        f"  Fold {fold.fold}: lookback={research.pullback_lookback}, "
                        f"ATR={research.atr_multiple:.1f}, "
                        f"return={selected.total_return:.2%}, "
                        f"DD={selected.max_drawdown:.2%}, trades={selected.trade_count}"
                    )
                stable = "一致" if rolling.stable_parameter_set else "不一致"
                print(
                    f"  样本外汇总: baseline={rolling.baseline_compounded_return:.2%}, "
                    f"selected={rolling.selected_compounded_return:.2%}, "
                    f"最差DD={rolling.selected_worst_drawdown:.2%}, "
                    f"盈利窗口={rolling.selected_positive_folds}/{len(rolling.folds)}, "
                    f"参数稳定性={stable}"
                )
        print("研究模式完成：未写入策略准入，未创建订单，LIVE_TRADING=false")
        return
    if args.command == "qualify-ema":
        # 调用幂等建表，确保首次运行时准入审计表已经存在且带有中文字段说明。
        create_schema()
        qualification_config = EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用经过质量检查的MySQL历史K线，禁止使用临时或未收盘行情审批策略。
                venue = _venue_for_symbol(contract)
                bars = load_market_bars(contract, interval, venue=venue)
                try:
                    # 调用三折滚动研究，准入只接受互不重叠的样本外结果。
                    rolling = run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        base_config=qualification_config,
                    )
                except InsufficientResearchData as exc:
                    print(f"{contract} {interval}: REJECTED（样本不足：{exc}）")
                    continue
                # 调用强制准入规则生成不可变决定，任何一项门槛失败都会拒绝。
                decision = evaluate_rolling_research(
                    rolling,
                    strategy_name="EMA_TREND",
                    strategy_version="1.0.0",
                )
                # 调用MySQL幂等保存方法，使用评估哈希防止重复审批记录。
                record_id = save_qualification(decision, venue=venue)
                status = "APPROVED" if decision.approved else "REJECTED"
                print(
                    f"{contract} {interval}: {status}, record_id={record_id}, "
                    f"return={decision.compounded_return:.2%}, "
                    f"worst_dd={decision.worst_drawdown:.2%}, "
                    f"positive_folds={decision.positive_folds}/{decision.total_folds}, "
                    f"min_trades={decision.minimum_fold_trades}"
                )
                print(f"  原因: {decision.reason}")
        return
    if args.command == "execution-safety":
        # 调用只读计数查询，不读取API密钥、账户余额或订单请求明细。
        safety = read_execution_safety_status(oanda_enabled=settings.oanda_enabled)
        print(f"Live trading: {settings.live_trading}")
        print(f"Approved qualifications: {safety.approved_qualifications}")
        print(f"Rejected qualifications: {safety.rejected_qualifications}")
        print(f"Active local orders: {safety.active_orders}")
        print(f"Open local positions: {safety.open_positions}")
        print(f"Paper runtime risk state: {safety.runtime_risk_state}")
        print(f"Gate account risk state: {safety.gate_runtime_risk_state}")
        print(
            "OANDA account risk state: "
            f"{safety.oanda_runtime_risk_state if settings.oanda_enabled else 'NOT_REQUIRED'}"
        )
        print(f"Healthy market streams: {safety.healthy_market_streams}")
        print(f"Unhealthy market streams: {safety.unhealthy_market_streams}")
        print(f"Saved strategy signals: {safety.strategy_signals}")
        print(f"Paper trade fills: {safety.paper_trades}")
        print(f"Closed paper positions: {safety.closed_paper_positions}")
        print(f"Paper account equity: {safety.paper_account_equity or 'NOT_INITIALIZED'}")
        # 当前Gate客户端没有任何下单方法，因此即使未来出现批准记录也不能发送订单。
        print("Exchange order submission available: False")
        return
    if args.command == "system-readiness":
        # 调用统一只读检查，不刷新心跳、不修改风控状态，也不启动任何服务。
        readiness = read_system_readiness(
            live_trading=settings.live_trading,
            oanda_enabled=settings.oanda_enabled,
        )
        print(f"System readiness: {readiness.evaluation.status}")
        print(
            f"Gate service: {readiness.gate_service_status}, "
            f"heartbeat_age={readiness.gate_heartbeat_age_seconds}"
        )
        if readiness.oanda_enabled:
            print(
                f"OANDA service: {readiness.oanda_service_status}, "
                f"heartbeat_age={readiness.oanda_heartbeat_age_seconds}"
            )
        else:
            print("OANDA service: PAUSED_BY_CONFIGURATION")
        print(
            f"Healthy market streams: {readiness.healthy_market_streams}/"
            f"{readiness.expected_market_streams}"
        )
        print(f"Gate account risk: {readiness.gate_risk_state}")
        print(
            "OANDA account risk: "
            f"{readiness.oanda_risk_state if readiness.oanda_enabled else 'NOT_REQUIRED'}"
        )
        print(f"Latest approved strategies: {readiness.approved_qualifications}")
        print(f"Paper simulation: {readiness.paper_simulation_status}")
        if readiness.evaluation.blockers:
            print("Blockers:")
            for blocker in readiness.evaluation.blockers:
                print(f"  - {blocker}")
        else:
            print("Blockers: none; manual review is allowed, live trading remains disabled")
        print("Live trading: False")
        print("Exchange order submission available: False")
        return
    if args.command == "risk-snapshot":
        # 调用幂等建表，首次运行时安全创建账户风控状态表。
        create_schema()
        # with结束时会调用客户端close；这里只读取测试网账户，不存在下单调用。
        with GateTestnetClient.from_settings(settings) as client:
            account = client.get_account()
        snapshot_time = datetime.now(UTC)
        # Gate total按账户权益口径保存，钱包余额扣除未实现盈亏后单独记录。
        balance = account.total - account.unrealised_pnl
        used_margin = account.position_margin + account.order_margin
        # 调用快照保存方法，财务字段全程使用Decimal，不经过float。
        snapshot_id = save_account_snapshot(
            venue="GATE_TESTNET",
            snapshot_time=snapshot_time,
            balance=balance,
            equity=account.total,
            available_margin=account.available,
            used_margin=used_margin,
            unrealized_pnl=account.unrealised_pnl,
        )
        # 调用运行时状态机，使用最新快照计算每日收益和历史峰值回撤。
        risk_state = evaluate_and_save_runtime_risk("GATE_TESTNET")
        print(f"Account snapshot saved: {snapshot_id}")
        print(f"Runtime risk state: {risk_state.state}")
        print(f"Daily return: {risk_state.daily_return:.2%}")
        print(f"Drawdown: {risk_state.drawdown:.2%}")
        print("Live trading: False")
        return
    if args.command in {"oanda-account-check", "oanda-risk-snapshot"}:
        if args.command == "oanda-risk-snapshot":
            # 调用幂等建表，确保首次运行可以保存OANDA专属账户快照和风控状态。
            create_schema()
        # 调用统一只读账户方法，不执行订单、持仓或账户修改请求。
        account, auto_selected = _read_oanda_practice_account(settings)
        print("OANDA Practice account access: OK")
        print(f"Account selection: {'AUTO_SINGLE' if auto_selected else 'CONFIGURED'}")
        print(f"Account currency: {account.currency}")
        print(f"Hedging enabled: {account.hedging_enabled}")
        if args.command == "oanda-account-check":
            try:
                # 调用账户级品种规则和报价，区分“能读K线”与“账户获准交易黄金”。
                with OandaPracticeClient.from_settings(settings) as client:
                    account_id, _ = resolve_oanda_account_id(
                        client, settings.oanda_practice_account_id
                    )
                    rules = client.get_instrument_rules(account_id, args.oanda_instrument)
                    price = client.get_price(account_id, args.oanda_instrument)
                print(
                    f"Gold paper execution capability: "
                    f"{'READY' if price.tradeable else 'BLOCKED_NOT_TRADEABLE'}"
                )
                print(f"Gold minimum units: {rules.minimum_trade_size}")
                print(f"Gold margin rate: {rules.margin_rate}")
            except OandaApiError as error:
                print("Gold paper execution capability: BLOCKED_ACCOUNT_INSTRUMENT")
                print(f"Reason: {error}")
            print("Exchange order submission available: False")
            return

        snapshot_time = datetime.now(UTC)
        # 调用统一资金快照保存，以NAV作为权益并使用OANDA返回的保证金字段。
        snapshot_id = save_account_snapshot(
            venue=OANDA_PRACTICE_VENUE,
            snapshot_time=snapshot_time,
            balance=account.balance,
            equity=account.nav,
            available_margin=account.margin_available,
            used_margin=account.margin_used,
            unrealized_pnl=account.unrealized_pl,
        )
        # 调用统一2%/8%状态机，为黄金账户建立独立且持久化的风控状态。
        risk_state = evaluate_and_save_runtime_risk(OANDA_PRACTICE_VENUE)
        print(f"Account snapshot saved: {snapshot_id}")
        print(f"Runtime risk state: {risk_state.state}")
        print(f"Daily return: {risk_state.daily_return:.2%}")
        print(f"Drawdown: {risk_state.drawdown:.2%}")
        print("Live trading: False")
        print("Exchange order submission available: False")
        return
    if args.command == "market-health":
        # 调用幂等建表，首次检查时安全创建行情健康状态表。
        create_schema()
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用行情健康检查，读取最新已收盘K线并写入本次心跳结果。
                health = refresh_market_health(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
                print(
                    f"{contract} {interval}: status={health.status}, "
                    f"stale_seconds={health.stale_seconds}, "
                    f"failures={health.consecutive_failures}"
                )
                print(f"  原因: {health.reason}")
        print("Exchange order submission available: False")
        return
    if args.command == "market-runner":
        if any(_venue_for_symbol(contract) != GATE_TESTNET_VENUE for contract in args.contracts):
            raise ValueError(
                "market-runner currently accepts Gate contracts only; "
                "use import-oanda-bars for XAU_USD"
            )
        # 调用幂等建表，首次启动时创建服务运行状态表及其他尚未存在的核心表。
        create_schema()
        runner_config = RunnerConfig(
            contracts=tuple(args.contracts),
            intervals=tuple(args.intervals),
            limit=args.limit,
            poll_seconds=args.poll_seconds,
            retry_initial_seconds=args.retry_initial_seconds,
            retry_max_seconds=args.retry_max_seconds,
            max_cycles=args.max_cycles,
        )
        stop_event = Event()

        def refresh_gate_risk_and_signals_after_market_cycle() -> None:
            """行情发布后先刷新Gate账户风控，再按开关运行模拟信号周期。"""
            # 调用只读测试网账户接口；客户端没有任何订单提交方法。
            with GateTestnetClient.from_settings(settings) as account_client:
                account = account_client.get_account()
            snapshot_time = datetime.now(UTC)
            balance = account.total - account.unrealised_pnl
            used_margin = account.position_margin + account.order_margin
            # 调用统一快照保存，让每轮信号使用最新权益和保证金状态。
            save_account_snapshot(
                venue=GATE_TESTNET_VENUE,
                snapshot_time=snapshot_time,
                balance=balance,
                equity=account.total,
                available_margin=account.available,
                used_margin=used_margin,
                unrealized_pnl=account.unrealised_pnl,
            )
            # 调用统一2%/8%状态机；失败或熔断结论均先于信号与订单门禁发布。
            risk_state = evaluate_and_save_runtime_risk(GATE_TESTNET_VENUE)
            print(
                f"Gate账户风控：{risk_state.state}，"
                f"当日收益{risk_state.daily_return:.2%}，回撤{risk_state.drawdown:.2%}"
            )
            if not args.enable_signals:
                return
            # 调用模拟信号周期；策略未批准和仓位换算未完成都会保持订单数为零。
            signal_summary = run_paper_signal_cycle(
                tuple(args.contracts),
                tuple(args.intervals),
                bar_limit=args.bar_limit,
                entry_executor=lambda **kwargs: execute_approved_paper_entry(
                    settings=settings,
                    **kwargs,
                ),
                exit_executor=close_position_for_ema_signal,
                position_monitor=lambda: monitor_paper_positions(bar_limit=args.bar_limit),
            )
            blocked_streams = sum(
                stream.status.startswith("BLOCKED") for stream in signal_summary.streams
            )
            print(
                f"信号观察完成：新增{signal_summary.new_signal_count}条，"
                f"阻断行情流{blocked_streams}条，订单{signal_summary.order_count}张"
            )
            # 调用30天监督器汇总完整UTC日；账户未初始化时不会提前启动计时。
            simulation = refresh_paper_simulation()
            if simulation is not None:
                print(
                    f"模拟监督：{simulation.status}，连续健康"
                    f"{simulation.consecutive_healthy_days}/{simulation.required_days}天"
                )

        # 调用行情运行器；每轮都刷新账户风控，只有显式开关才追加EMA信号观察。
        runner = MarketDataRunner(
            settings,
            runner_config,
            stop_event=stop_event,
            reporter=print,
            after_cycle=refresh_gate_risk_and_signals_after_market_cycle,
        )
        print("行情服务启动；仅连接Gate测试网，真实交易始终关闭")
        # 文件锁阻止本机重复进程；信号处理器让Ctrl+C和Docker停止都能安全收尾。
        with SingleInstanceLock(args.lock_file), install_shutdown_signal_handlers(stop_event):
            result = runner.run()
        print(f"行情服务已安全停止，成功轮询{result.successful_cycles}轮")
        print("Exchange order submission available: False")
        return
    if args.command == "market-runner-status":
        # 调用只读状态查询，服务从未启动时明确显示缺失而不假定其健康。
        state = read_service_state(MARKET_DATA_SERVICE_NAME)
        if state is None:
            print("Market data runner has never started")
        else:
            print(f"Service status: {state.status}")
            print(f"Process ID: {state.process_id}")
            print(f"Successful cycles: {state.successful_cycles}")
            print(f"Consecutive failures: {state.consecutive_failures}")
            print(f"Heartbeat UTC: {state.heartbeat_at}")
            print(f"Last error: {state.last_error or '-'}")
        print("Exchange order submission available: False")
        return
    if args.command == "oanda-runner":
        if not settings.oanda_enabled:
            print("OANDA runner is paused; set OANDA_ENABLED=true only when gold resumes")
            print("Live trading: False")
            print("Exchange order submission available: False")
            return
        # 调用幂等建表，确保OANDA服务心跳和行情健康状态可以安全保存。
        create_schema()
        oanda_runner_config = RunnerConfig(
            contracts=(args.oanda_instrument,),
            intervals=tuple(args.intervals),
            limit=args.limit,
            poll_seconds=args.poll_seconds,
            retry_initial_seconds=args.retry_initial_seconds,
            retry_max_seconds=args.retry_max_seconds,
            max_cycles=args.max_cycles,
        )
        oanda_stop_event = Event()

        def import_oanda_runner_cycle(
            client: OandaPracticeClient,
            *,
            contracts: tuple[str, ...],
            intervals: tuple[str, ...],
            limit: int,
        ) -> object:
            """把通用轮询参数转换为单一OANDA黄金品种导入调用。"""
            # 调用OANDA幂等导入；长期轮询只刷新最近窗口，不执行历史回溯。
            return import_oanda_history(
                client,
                instrument=contracts[0],
                intervals=intervals,
                limit=limit,
            )

        def refresh_oanda_runner_health(symbol: str, interval: str) -> object:
            """使用OANDA交易场所刷新黄金行情流健康状态。"""
            # 调用统一健康检查并显式指定Practice，避免误读同名其他交易场所。
            return refresh_market_health(
                symbol,
                interval,
                venue=OANDA_PRACTICE_VENUE,
            )

        def refresh_oanda_risk_and_signals_after_market_cycle() -> None:
            """行情发布后刷新风控，并让唯一主周期运行本地模拟成交。"""
            # 调用只读账户摘要并写入独立权益快照；账户失败会让本轮进入RETRYING。
            account, _auto_selected = _read_oanda_practice_account(settings)
            snapshot_time = datetime.now(UTC)
            save_account_snapshot(
                venue=OANDA_PRACTICE_VENUE,
                snapshot_time=snapshot_time,
                balance=account.balance,
                equity=account.nav,
                available_margin=account.margin_available,
                used_margin=account.margin_used,
                unrealized_pnl=account.unrealized_pl,
            )
            # 调用统一状态机，先发布2%/8%风控结论再考虑策略信号。
            risk_state = evaluate_and_save_runtime_risk(OANDA_PRACTICE_VENUE)
            print(
                f"OANDA账户风控：{risk_state.state}，"
                f"当日收益{risk_state.daily_return:.2%}，回撤{risk_state.drawdown:.2%}"
            )
            if not args.enable_signals:
                return
            try:
                # 调用账户级黄金规则和报价；仅能读取公共K线不代表该账户可模拟执行黄金。
                with OandaPracticeClient.from_settings(settings) as capability_client:
                    account_id, _ = resolve_oanda_account_id(
                        capability_client, settings.oanda_practice_account_id
                    )
                    capability_client.get_instrument_rules(
                        account_id, args.oanda_instrument
                    )
                    current_price = capability_client.get_price(
                        account_id, args.oanda_instrument
                    )
            except OandaApiError as error:
                print(f"黄金模拟执行门禁：BLOCKED_ACCOUNT_INSTRUMENT，原因：{error}")
                return
            if not current_price.tradeable:
                print("黄金模拟执行门禁：BLOCKED_NOT_TRADEABLE")
                return
            # 调用黄金独立模拟账本；首次固定Practice净值，之后每轮只刷新本地权益。
            refresh_paper_account(
                now=snapshot_time,
                initial_equity=account.nav,
                paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
            )
            summaries = []
            primary_intervals = tuple(
                interval
                for interval in args.intervals
                if interval == args.oanda_paper_interval
            )
            if primary_intervals:
                # 调用黄金主周期闭环；只读Practice接口提供报价和规则，成交只写本地数据库。
                summaries.append(
                    run_paper_signal_cycle(
                        (args.oanda_instrument,),
                        primary_intervals,
                        bar_limit=args.bar_limit,
                        venue=OANDA_PRACTICE_VENUE,
                        entry_executor=lambda **kwargs: execute_approved_oanda_paper_entry(
                            settings=settings,
                            primary_interval=args.oanda_paper_interval,
                            **kwargs,
                        ),
                        exit_executor=lambda **kwargs: close_position_for_ema_signal(
                            paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
                            source_venue=OANDA_PRACTICE_VENUE,
                            **kwargs,
                        ),
                        position_monitor=lambda: monitor_paper_positions(
                            bar_limit=args.bar_limit,
                            paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
                            source_venue=OANDA_PRACTICE_VENUE,
                        ),
                    )
                )
            shadow_intervals = tuple(
                interval
                for interval in args.intervals
                if interval != args.oanda_paper_interval
            )
            if shadow_intervals:
                # 调用影子周期仅保存信号；不提供执行器，因此不会争抢同一黄金仓位。
                summaries.append(
                    run_paper_signal_cycle(
                        (args.oanda_instrument,),
                        shadow_intervals,
                        bar_limit=args.bar_limit,
                        venue=OANDA_PRACTICE_VENUE,
                    )
                )
            streams = tuple(
                stream for cycle_summary in summaries for stream in cycle_summary.streams
            )
            blocked_streams = sum(
                stream.status.startswith("BLOCKED") for stream in streams
            )
            new_signal_count = sum(summary.new_signal_count for summary in summaries)
            order_count = sum(summary.order_count for summary in summaries)
            print(
                f"黄金信号观察完成：新增{new_signal_count}条，"
                f"阻断行情流{blocked_streams}条，本地模拟订单{order_count}张"
            )
            # 调用OANDA专属30天监督器，黄金权益与OANDA服务心跳均不混入Gate记录。
            simulation = refresh_paper_simulation(
                paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
                market_service_name=OANDA_MARKET_DATA_SERVICE_NAME,
            )
            if simulation is not None:
                print(
                    f"黄金模拟监督：{simulation.status}，连续健康"
                    f"{simulation.consecutive_healthy_days}/{simulation.required_days}天"
                )

        oanda_runner = MarketDataRunner(
            settings,
            oanda_runner_config,
            stop_event=oanda_stop_event,
            client_factory=lambda: OandaPracticeClient.from_settings(settings),
            importer=import_oanda_runner_cycle,  # type: ignore[arg-type]
            health_refresher=refresh_oanda_runner_health,
            after_cycle=refresh_oanda_risk_and_signals_after_market_cycle,
            reporter=print,
            service_name=OANDA_MARKET_DATA_SERVICE_NAME,
        )
        print("OANDA黄金行情服务启动；仅连接Practice，真实交易始终关闭")
        # 调用独立文件锁和信号处理器，确保不与Gate服务争用锁且停止时完整收尾。
        with SingleInstanceLock(args.oanda_lock_file), install_shutdown_signal_handlers(
            oanda_stop_event
        ):
            result = oanda_runner.run()
        print(f"OANDA黄金行情服务已安全停止，成功轮询{result.successful_cycles}轮")
        print("Exchange order submission available: False")
        return
    if args.command == "oanda-runner-status":
        # 调用OANDA独立服务名查询，绝不把Gate心跳当作黄金行情健康证据。
        state = read_service_state(OANDA_MARKET_DATA_SERVICE_NAME)
        if state is None:
            print("OANDA market data runner has never started")
        else:
            print(f"Service status: {state.status}")
            print(f"Process ID: {state.process_id}")
            print(f"Successful cycles: {state.successful_cycles}")
            print(f"Consecutive failures: {state.consecutive_failures}")
            print(f"Heartbeat UTC: {state.heartbeat_at}")
            print(f"Last error: {state.last_error or '-'}")
        print("Live trading: False")
        print("Exchange order submission available: False")
        return
    if args.command == "paper-signal-cycle":
        # 调用幂等建表，确保信号、准入和行情健康状态表均已存在。
        create_schema()
        # 调用模拟信号单轮主循环；该方法只保存信号，不构造或提交交易所订单。
        summary = run_paper_signal_cycle(
            tuple(args.contracts),
            tuple(args.intervals),
            bar_limit=args.bar_limit,
            entry_executor=lambda **kwargs: execute_approved_paper_entry(
                settings=settings,
                **kwargs,
            ),
            exit_executor=close_position_for_ema_signal,
            position_monitor=lambda: monitor_paper_positions(bar_limit=args.bar_limit),
        )
        for stream in summary.streams:
            print(
                f"{stream.symbol} {stream.interval}: status={stream.status}, "
                f"actions={stream.action_count}, new_signals={stream.new_signal_count}, "
                f"orders={stream.order_count}"
            )
            print(f"  原因: {stream.reason}")
        print(f"New strategy signals: {summary.new_signal_count}")
        print(f"Paper orders created: {summary.order_count}")
        # 调用30天监督器刷新日度指标；该结论只用于人工复核，不开启真实交易。
        simulation = refresh_paper_simulation()
        if simulation is None:
            print("Paper simulation: NOT_STARTED")
        else:
            print(
                f"Paper simulation: {simulation.status}, healthy_days="
                f"{simulation.consecutive_healthy_days}/{simulation.required_days}"
            )
        print("Exchange order submission available: False")
        return
    if args.command in {"paper-simulation-status", "oanda-paper-simulation-status"}:
        # 调用只读监督状态查询，不因查看状态而启动或延长30天计时。
        paper_venue = (
            OANDA_PAPER_ACCOUNT_VENUE
            if args.command == "oanda-paper-simulation-status"
            else "GATE_TESTNET_PAPER"
        )
        simulation = read_paper_simulation_state(paper_venue=paper_venue)
        if simulation is None:
            print("Paper simulation: NOT_STARTED")
            print("Reason: 模拟账户尚未初始化，30天稳定运行计时尚未开始")
        else:
            print(f"Paper simulation: {simulation.status}")
            print(
                f"Consecutive healthy days: {simulation.consecutive_healthy_days}/"
                f"{simulation.required_days}"
            )
            print(f"Completed UTC days: {simulation.elapsed_days}")
            print(f"Current equity: {simulation.current_equity}")
            print(f"Total return: {simulation.total_return:.2%}")
            print(f"Maximum drawdown: {simulation.maximum_drawdown:.2%}")
            print(f"Service incidents: {simulation.service_incident_count}")
            print(f"Reason: {simulation.reason}")
        print("Live trading: False")
        print("Exchange order submission available: False")
        return

    # 默认命令只输出非敏感配置，禁止打印数据库密码或 API 密钥。
    print("Gold Crypto Quant is ready")
    print(f"Environment: {settings.app_env}")
    print(f"Symbols: {', '.join(settings.symbols)}")
    print(f"Intervals: {', '.join(settings.intervals)}")
    print(f"Live trading: {settings.live_trading}")
