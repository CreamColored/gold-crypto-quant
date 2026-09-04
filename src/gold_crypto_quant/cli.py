"""项目命令行入口。

这里仅提供无交易风险的管理命令，不包含下单功能。
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

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
from gold_crypto_quant.notifications import RuntimeEventNotifier
from gold_crypto_quant.risk.qualification import evaluate_rolling_research
from gold_crypto_quant.runtime import (
    MARKET_DATA_SERVICE_NAME,
    OANDA_MARKET_DATA_SERVICE_NAME,
    MarketDataRunner,
    RunnerConfig,
    SingleInstanceLock,
    install_shutdown_signal_handlers,
    run_bollinger_signal_cycle,
    run_paper_signal_cycle,
)
from gold_crypto_quant.runtime.oanda_paper_entry import execute_approved_oanda_paper_entry
from gold_crypto_quant.runtime.public_market_comparison_runner import (
    PublicMarketComparisonRunner,
)
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
from gold_crypto_quant.strategy.bollinger_range import parameters_for_same_timeframe


def _venue_for_symbol(symbol: str) -> str:
    """根据第一阶段唯一品种代码选择测试交易场所。"""
    return OANDA_PRACTICE_VENUE if symbol == "XAU_USD" else GATE_TESTNET_VENUE


def _runtime_log(message: str) -> None:
    """为长期运行服务输出带本机时区的可读时间戳。"""
    # 调用系统本地时区转换，Mac与后续Ubuntu部署都无需硬编码Asia/Shanghai。
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    # 调用立即刷新，确保终端或重定向日志在进程异常时仍保留最后一条完整记录。
    print(f"[{timestamp}] {message}", flush=True)


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


# 回测模块拖着 vectorbt、numba、llvmlite、scipy、plotly 一共约 330 MB，而三个线上
# 服务一行都不用。放在模块顶层会让部署机器为了跑服务被迫装下整套回测依赖——
# 那台 2 核 2G、磁盘只有 8.4G 的服务器装不下，也没必要。
#
# 改成用到时才导入：服务启动更快，部署只需运行时依赖。
def _backtest():
    """按需加载回测模块；只有回测子命令会走到这里。"""
    from gold_crypto_quant import backtest as module
    from gold_crypto_quant.backtest import bollinger_range

    return module, bollinger_range


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
            "backtest-bollinger",
            "qualify-bollinger",
            "backtest-ema",
            "diagnose-ema",
            "research-ema",
            "walkforward-ema",
            "walkforward-filters",
            "walkforward-pullback",
            "walkforward-regime",
            "walkforward-ema12",
            "walkforward-breakout",
            "walkforward-joint-breakout",
            "walkforward-joint-exits",
            "walkforward-joint-regime",
            "walkforward-joint-macro",
            "qualify-ema",
            "qualify-breakout-fixed",
            "execution-safety",
            "system-readiness",
            "risk-snapshot",
            "market-health",
            "market-runner",
            "market-runner-status",
            "public-market-comparison",
            "strategy-comparison",
            "quote-collector",
            "web-init-admin",
            "web-run",
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
        "--log-every-cycle",
        action="store_true",
        help=(
            "每一轮都写日志。1秒轮询下一天86400行，其中绝大多数是"
            "什么都没发生；只在盯盘或排查时开。"
        ),
    )
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
        help="在长期行情服务每轮成功后运行布林带信号观察；实盘提交始终关闭",
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
    if args.command == "web-init-admin":
        # 只创建缺失表和唯一admin；重复执行绝不重置已有管理员密码。
        from gold_crypto_quant.storage.shadow_monitor import ensure_system_shadow_accounts
        from gold_crypto_quant.storage.web_admin import create_admin_if_missing

        create_schema()
        ensure_system_shadow_accounts()
        initial_password = create_admin_if_missing()
        if initial_password is None:
            print("Admin account already exists; password was not changed")
        else:
            print("Admin account created")
            print("Username: admin")
            print(f"One-time password: {initial_password}")
            print("Change this password immediately after the first login")
        return
    if args.command == "web-run":
        # Web后台只有只读查询和admin安全设置，不包含交易、撤单或策略修改接口。
        import uvicorn

        from gold_crypto_quant.web import create_app

        app = create_app()
        if app.state.initial_admin_password:
            _runtime_log("首次admin已创建")
            print("Username: admin")
            print(f"One-time password: {app.state.initial_admin_password}")
            print("Change this password immediately after the first login")
        _runtime_log(
            f"Web监管后台启动：http://{settings.web_host}:{settings.web_port}；"
            "admin只读，真实交易关闭"
        )
        uvicorn.run(
            app,
            host=settings.web_host,
            port=settings.web_port,
            access_log=False,
        )
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
    if args.command in {"backtest-bollinger", "qualify-bollinger"}:
        # 新策略的固定点数参数只为ETH设计；BTC必须另做尺度化研究，不能直接套用。
        unsupported = [contract for contract in args.contracts if contract != "ETH_USDT"]
        if unsupported:
            print(f"跳过非ETH品种：{', '.join(unsupported)}；固定点数参数不能跨品种复用")
        # Gate测试网只开放有限数量的5分钟历史；读取最近连续窗口，避免旧数据断层污染结果。
        bars_5m = load_market_bars("ETH_USDT", "5m", limit=1999)
        all_bars_15m = load_market_bars("ETH_USDT", "15m")
        # 两个周期使用同一个近七天起点，保证最终结果可以直接横向比较。
        bars_15m = all_bars_15m.loc[all_bars_15m.index >= bars_5m.index[0]]
        bollinger_config = _backtest()[1].BollingerBacktestConfig(
            initial_equity=args.initial_equity
        )
        if args.command == "backtest-bollinger":
            for interval, bars in (("15m", bars_15m), ("5m", bars_5m)):
                # 调用同周期因果回测：本周期确认震荡、触轨，并在本周期下一根开盘执行。
                result = _backtest()[1].run_bollinger_backtest(
                    bars,
                    bars,
                    symbol="ETH_USDT",
                    parameters=parameters_for_same_timeframe(interval),
                    config=bollinger_config,
                    same_timeframe=True,
                )
                print(
                    f"ETH_USDT {interval}同周期: return={result.total_return:.2%}, "
                    f"DD={result.max_drawdown:.2%}, trades={result.trade_count}, "
                    f"win_rate={result.win_rate:.2%}, PF={result.profit_factor:.2f}"
                )
                print(
                    f"  止损={result.stop_count}, 中轨减仓={result.middle_reduction_count}, "
                    f"对侧轨止盈={result.opposite_band_exit_count}"
                )
            print("Live trading: False")
            print("Exchange order submission available: False")
            return
        # 调用固定参数三折准入；失败结果同样保存，确保不能靠删除失败记录绕过门禁。
        for interval, bars in (("15m", bars_15m), ("5m", bars_5m)):
            qualification = _backtest()[1].qualify_fixed_bollinger_strategy(
                bars,
                bars,
                symbol="ETH_USDT",
                parameters=parameters_for_same_timeframe(interval),
                config=bollinger_config,
                interval=interval,
                same_timeframe=True,
            )
            decision = _backtest()[1].build_bollinger_qualification_decision(qualification)
            record_id = save_qualification(decision)
            for fold_number, fold in enumerate(qualification.folds, start=1):
                print(
                    f"  {interval} Fold {fold_number}: return={fold.total_return:.2%}, "
                    f"DD={fold.max_drawdown:.2%}, trades={fold.trade_count}, "
                    f"win_rate={fold.win_rate:.2%}, PF={fold.profit_factor:.2f}"
                )
            print(
                f"ETH_USDT {interval}同周期: "
                f"{'APPROVED' if qualification.approved else 'REJECTED'}, "
                f"record_id={record_id}, return={qualification.compounded_return:.2%}, "
                f"worst_dd={qualification.worst_drawdown:.2%}, "
                f"positive_folds={qualification.positive_folds}/3, "
                f"min_trades={qualification.minimum_fold_trades}"
            )
            print(f"  原因: {qualification.reason}")
        print("Live trading: False")
        print("Exchange order submission available: False")
        return
    if args.command == "backtest-ema":
        # 创建统一回测参数；125倍是名义仓位硬上限，实际仓位仍由单笔风险决定。
        backtest_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 从MySQL只读取已经收盘且按时间升序排列的K线。
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                # 运行EMA 20/50/200多空回测；该调用只在内存中计算，不连接交易接口。
                result, _portfolio = _backtest()[0].run_ema_backtest(
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
        diagnostic_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 从MySQL调用标准行情读取方法，读取前会检查连续性和OHLC合法性。
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                # 先调用正式回测，确保诊断分析的是风控生效后的真实交易序列。
                _result, portfolio = _backtest()[0].run_ema_backtest(
                    bars,
                    symbol=contract,
                    interval=interval,
                    config=diagnostic_config,
                )
                # 调用逐笔诊断方法，拆分交易方向、成本、退出类型和进场市场状态。
                diagnostic = _backtest()[0].diagnose_trades(
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
        research_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，研究前先执行连续性和OHLC质量检查。
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                # 调用时间隔离研究：前70%选择参数，后30%仅做一次样本外验证。
                research = _backtest()[0].run_holdout_research(
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
        walkforward_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，所有滚动窗口共享同一份已验证历史数据。
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                # 调用三折扩展窗口研究，每折验证数据都不会参与该折参数选择。
                try:
                    rolling = _backtest()[0].run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        base_config=walkforward_config,
                    )
                except _backtest()[0].InsufficientResearchData as exc:
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
        filter_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，保证滚动研究只使用连续且合法的已收盘K线。
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                try:
                    # 调用三折滚动研究，同时评估ADX、高周期确认、冷却期、方向和ATR。
                    # both、long、short必须同时保留，避免研究阶段先验偏向某一个交易方向。
                    rolling = _backtest()[0].run_rolling_research(
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
                except _backtest()[0].InsufficientResearchData as exc:
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
        pullback_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，确保回踩触碰使用的是完整连续的最高价和最低价。
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                try:
                    # 调用滚动研究，交叉基线只保留一组，回踩分别研究2、3、5根观察窗口。
                    # 同时保留both、long、short，确保BTC做多候选不会被研究入口提前排除。
                    rolling = _backtest()[0].run_rolling_research(
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
                except _backtest()[0].InsufficientResearchData as exc:
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
        regime_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，所有判断仅基于按时间排序的已收盘K线。
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                try:
                    # 调用三折滚动研究；每折只在训练集比较斜率开关和三档ATR距离。
                    # 方向固定both，避免再次通过历史收益选择固定做多或固定做空。
                    rolling = _backtest()[0].run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        trend_strengths=(0.0,),
                        atr_multiples=(1.5, 2.0, 2.5),
                        directions=("both",),
                        trend_slope_lookbacks=(0, 5, 10),
                        base_config=regime_config,
                    )
                except _backtest()[0].InsufficientResearchData as exc:
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
        ema12_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
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
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                try:
                    # 调用三折滚动研究；只比较ATR止损和1/2/3根回踩观察窗，多空始终同时开启。
                    rolling = _backtest()[0].run_rolling_research(
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
                except _backtest()[0].InsufficientResearchData as exc:
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
    if args.command == "walkforward-breakout":
        # 突破研究只替换入场条件，继续复用EMA趋势方向、ATR仓位、成本和两级熔断。
        breakout_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        breakout_strategy = EmaTrendParameters(
            entry_mode="breakout",
            pullback_lookback=20,
        )
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用标准行情读取方法，历史区间完整性仍由统一存储层检查。
                bars = load_market_bars(contract, interval, venue=_venue_for_symbol(contract))
                try:
                    # 调用三折滚动研究，仅比较10/20/40根突破窗口和三档ATR止损。
                    # 方向固定both，确保最终策略始终符合BTC和ETH多空都做的核心规则。
                    rolling = _backtest()[0].run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        trend_strengths=(0.0,),
                        atr_multiples=(1.5, 2.0, 2.5),
                        directions=("both",),
                        entry_variants=(
                            ("breakout", 10),
                            ("breakout", 20),
                            ("breakout", 40),
                        ),
                        base_strategy=breakout_strategy,
                        base_config=breakout_config,
                    )
                except _backtest()[0].InsufficientResearchData as exc:
                    print(f"{contract} {interval}: 样本不足，{exc}")
                    continue
                candidate_count = rolling.folds[0].research.candidate_count
                print(f"{contract} {interval}: breakout_candidates={candidate_count}")
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
    if args.command in {
        "walkforward-joint-breakout",
        "walkforward-joint-exits",
        "walkforward-joint-regime",
        "walkforward-joint-macro",
    }:
        if len(args.contracts) < 2:
            raise ValueError("共同突破研究至少需要BTC和ETH两个品种")
        joint_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        research_exits = args.command == "walkforward-joint-exits"
        research_regime = args.command in {
            "walkforward-joint-regime",
            "walkforward-joint-macro",
        }
        research_macro = args.command == "walkforward-joint-macro"
        for interval in args.intervals:
            bars_by_symbol = {}
            for contract in args.contracts:
                # 为同一周期调用标准行情读取方法，两个品种仍保留各自完整的时间顺序。
                bars_by_symbol[contract] = load_market_bars(
                    contract, interval, venue=_venue_for_symbol(contract)
                )
            try:
                # 调用共同选参研究；训练得分取所有品种中最差者，防止牺牲ETH换取BTC收益。
                joint = _backtest()[0].run_joint_breakout_research(
                    bars_by_symbol,
                    interval=interval,
                    lookbacks=(20, 40) if research_exits or research_regime else (10, 20, 40),
                    atr_multiples=(1.5, 2.0, 2.5),
                    take_profit_atr_multiples=(0.0, 2.0, 3.0) if research_exits else (0.0,),
                    trailing_options=(False, True) if research_exits else (False,),
                    adx_thresholds=(20.0, 25.0) if research_regime else (0.0,),
                    higher_timeframe_filters=(True,) if research_regime else (False,),
                    trend_slope_lookbacks=(5, 10) if research_regime else (0,),
                    base_strategy=EmaTrendParameters(
                        entry_mode="breakout",
                        pullback_lookback=20,
                        higher_timeframe_mode="macro" if research_macro else "standard",
                    ),
                    base_config=joint_config,
                )
            except _backtest()[0].InsufficientResearchData as exc:
                print(f"{interval}: 共同样本不足，{exc}")
                continue
            print(
                f"{interval}: joint_breakout_candidates={joint.candidate_count}, "
                f"symbols={','.join(joint.symbols)}"
            )
            for fold in joint.folds:
                details = ", ".join(
                    f"{symbol}={fold.test_results[symbol].total_return:.2%}/"
                    f"DD {fold.test_results[symbol].max_drawdown:.2%}/"
                    f"{fold.test_results[symbol].trade_count}笔"
                    for symbol in joint.symbols
                )
                print(
                    f"  Fold {fold.fold}: lookback={fold.lookback}, "
                    f"止损ATR={fold.atr_multiple:.1f}, "
                    f"止盈ATR={fold.take_profit_atr_multiple:.1f}, "
                    f"移动止损={'on' if fold.trailing_stop else 'off'}, {details}"
                )
                if research_regime:
                    print(
                        f"    市场状态: ADX>={fold.min_adx:.0f}, "
                        f"高周期={'on' if fold.use_higher_timeframe_filter else 'off'}, "
                        f"层级={fold.higher_timeframe_mode}, "
                        f"EMA200斜率回看={fold.trend_slope_lookback}"
                    )
            stable = "一致" if joint.stable_parameter_set else "不一致"
            for symbol in joint.symbols:
                minimum_trades = min(fold.test_results[symbol].trade_count for fold in joint.folds)
                passed = (
                    joint.compounded_returns[symbol] > 0
                    and joint.worst_drawdowns[symbol] < joint_config.max_drawdown_limit
                    and joint.positive_folds[symbol] >= 2
                    and minimum_trades >= 8
                )
                print(
                    f"  {symbol}汇总: return={joint.compounded_returns[symbol]:.2%}, "
                    f"最差DD={joint.worst_drawdowns[symbol]:.2%}, "
                    f"盈利窗口={joint.positive_folds[symbol]}/{len(joint.folds)}, "
                    f"最少交易={minimum_trades}, 准入={'通过' if passed else '拒绝'}"
                )
            print(f"  共同参数稳定性={stable}")
        print("共同研究完成：未写入策略准入，未创建订单，LIVE_TRADING=false")
        return
    if args.command == "qualify-ema":
        # 调用幂等建表，确保首次运行时准入审计表已经存在且带有中文字段说明。
        create_schema()
        qualification_config = _backtest()[0].EmaBacktestConfig(initial_equity=args.initial_equity)
        for contract in args.contracts:
            for interval in args.intervals:
                # 调用经过质量检查的MySQL历史K线，禁止使用临时或未收盘行情审批策略。
                venue = _venue_for_symbol(contract)
                bars = load_market_bars(contract, interval, venue=venue)
                try:
                    # 调用三折滚动研究，准入只接受互不重叠的样本外结果。
                    rolling = _backtest()[0].run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        base_config=qualification_config,
                    )
                except _backtest()[0].InsufficientResearchData as exc:
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
    if args.command == "qualify-breakout-fixed":
        # 15根突破是对20根基准的温和放宽；它已单独通过BTC和ETH三折样本外硬门槛。
        # 准入过程仍只接受这一组固定参数，禁止根据验证窗口临时选择更好看的结果。
        create_schema()
        fixed_strategy = EmaTrendParameters(
            entry_mode="breakout",
            pullback_lookback=15,
            min_adx=25.0,
            use_higher_timeframe_filter=True,
            higher_timeframe_mode="standard",
            trend_slope_lookback=5,
        )
        fixed_config = _backtest()[0].EmaBacktestConfig(
            initial_equity=args.initial_equity,
            atr_multiple=1.5,
        )
        for contract in args.contracts:
            for interval in args.intervals:
                if interval != "30m":
                    print(f"{contract} {interval}: 跳过，固定突破策略只批准30m")
                    continue
                venue = _venue_for_symbol(contract)
                # 调用完整历史读取，确保正式准入与共同研究使用同一份连续已收盘K线。
                bars = load_market_bars(contract, interval, venue=venue)
                try:
                    # 每折只提供一个固定候选；训练或验证结果都不能改变任何策略参数。
                    rolling = _backtest()[0].run_rolling_research(
                        bars,
                        symbol=contract,
                        interval=interval,
                        trend_strengths=(0.0,),
                        atr_multiples=(1.5,),
                        directions=("both",),
                        adx_thresholds=(25.0,),
                        higher_timeframe_filters=(True,),
                        trend_slope_lookbacks=(5,),
                        cooldown_options=(0,),
                        entry_variants=(("breakout", 15),),
                        base_strategy=fixed_strategy,
                        base_config=fixed_config,
                    )
                except _backtest()[0].InsufficientResearchData as exc:
                    print(f"{contract} {interval}: REJECTED（样本不足：{exc}）")
                    continue
                # 调用统一硬门禁，收益、回撤、盈利窗口或交易数任一失败都会拒绝。
                decision = evaluate_rolling_research(
                    rolling,
                    strategy_name="EMA_TREND",
                    strategy_version="1.0.0",
                )
                # 调用幂等保存；相同历史和参数重跑只更新同一个评估哈希记录。
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
        print("固定突破准入完成：只授权本地模拟，LIVE_TRADING=false")
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
    if args.command == "quote-collector":
        # 只采集盘口，不参与任何交易判定；策略仍按已收盘K线运行。
        import asyncio

        from gold_crypto_quant.runtime.quote_collector import QuoteCollector

        create_schema()
        collector = QuoteCollector(reporter=_runtime_log)
        _runtime_log("盘口采集：Gate与币安 bookTicker，按秒与按分钟聚合入库")
        try:
            asyncio.run(collector.run())
        except KeyboardInterrupt:
            _runtime_log("收到中断信号，正在冲刷剩余数据")
        return
    if args.command == "strategy-comparison":
        from gold_crypto_quant.runtime.strategy_v1_runner import run_strategy_comparison

        run_strategy_comparison(
            poll_seconds=args.poll_seconds,
            max_cycles=args.max_cycles,
            log_every_cycle=args.log_every_cycle,
        )
        return 0

    if args.command == "public-market-comparison":
        # 两个实盘公共行情源只进入隔离的本地影子账户，不读取任何交易所API密钥。
        create_schema()
        comparison_stop = Event()
        runner = PublicMarketComparisonRunner(
            settings,
            poll_seconds=args.poll_seconds,
            limit=args.limit,
            max_cycles=args.max_cycles or 10_080,
            stop_event=comparison_stop,
            reporter=_runtime_log,
            log_every_cycle=args.log_every_cycle,
        )
        _runtime_log("实盘公共行情对照启动；真实交易始终关闭")
        with (
            SingleInstanceLock(Path(".runtime/public-market-comparison.lock")),
            install_shutdown_signal_handlers(comparison_stop),
        ):
            cycles = runner.run()
        _runtime_log(f"Gate/币安公共行情对照已安全停止，完成{cycles}轮")
        _runtime_log("Exchange order submission available: False")
        return
    if args.command == "market-runner":
        if any(_venue_for_symbol(contract) != GATE_TESTNET_VENUE for contract in args.contracts):
            raise ValueError(
                "market-runner currently accepts Gate contracts only; "
                "use import-oanda-bars for XAU_USD"
            )
        # 调用幂等建表，首次启动时创建服务运行状态表及其他尚未存在的核心表。
        create_schema()
        # 开启布林带影子信号时额外采集1分钟；它只用于开仓过滤，不进入交易周期优先级。
        runner_intervals = tuple(
            dict.fromkeys((*args.intervals, *(("1m",) if args.enable_signals else ())))
        )
        runner_config = RunnerConfig(
            contracts=tuple(args.contracts),
            intervals=runner_intervals,
            limit=args.limit,
            poll_seconds=args.poll_seconds,
            retry_initial_seconds=args.retry_initial_seconds,
            retry_max_seconds=args.retry_max_seconds,
            max_cycles=args.max_cycles,
        )
        stop_event = Event()
        # 创建事件通知器；不再按固定四小时发送，只在成交或重要状态变化时发送。
        event_notifier = RuntimeEventNotifier(settings, reporter=_runtime_log)
        previous_gate_risk_state: str | None = None
        previous_signal_status: str | None = None

        # 启停属于日常操作，每次重启都推会让真正的异常淹在里面；异常与恢复照常推。
        SILENT_LIFECYCLE_EVENTS = {"SERVICE_STARTED", "SERVICE_STOPPED"}

        def report_runtime_event(event: str, detail: str) -> None:
            """把行情服务异常事件转换为即时邮件，重复重试不会逐分钟刷屏。"""
            titles = {
                "SERVICE_STARTED": ("Gate行情服务启动", "INFO"),
                "SERVICE_RETRYING": ("Gate行情服务异常", "CRITICAL"),
                "SERVICE_RECOVERED": ("Gate行情服务恢复", "RECOVERED"),
                "SERVICE_STOPPED": ("Gate行情服务停止", "WARNING"),
            }
            title, severity = titles.get(event, (event, "WARNING"))
            if event in SILENT_LIFECYCLE_EVENTS:
                _runtime_log(f"{title}（启停不推送）：{detail}")
                return
            sent = event_notifier.send(
                event_key=f"runtime:{event}",
                event_title=title,
                event_lines=(f"详情：{detail}",),
                severity=severity,
                repeatable=event in {"SERVICE_RETRYING", "SERVICE_RECOVERED"},
            )
            if sent:
                _runtime_log(f"事件邮件已发送：{title}")

        def refresh_gate_risk_and_signals_after_market_cycle() -> None:
            """行情发布后先刷新Gate账户风控，再按开关运行模拟信号周期。"""
            nonlocal previous_gate_risk_state, previous_signal_status
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
            _runtime_log(
                f"Gate账户风控：{risk_state.state}，"
                f"当日收益{risk_state.daily_return:.2%}，回撤{risk_state.drawdown:.2%}"
            )
            if risk_state.state != previous_gate_risk_state and (
                previous_gate_risk_state is not None or risk_state.state != "NORMAL"
            ):
                # 调用事件邮件；熔断立即告警，恢复为NORMAL时也发送恢复通知。
                event_notifier.send(
                    event_key=f"gate-risk:{previous_gate_risk_state}->{risk_state.state}",
                    event_title=(
                        "Gate账户风控恢复" if risk_state.state == "NORMAL" else "Gate账户触发风控"
                    ),
                    event_lines=(
                        f"原状态：{previous_gate_risk_state or '-'}",
                        f"新状态：{risk_state.state}",
                        f"当日收益：{risk_state.daily_return:.2%}",
                        f"历史回撤：{risk_state.drawdown:.2%}",
                    ),
                    severity="RECOVERED" if risk_state.state == "NORMAL" else "CRITICAL",
                    repeatable=True,
                )
            previous_gate_risk_state = risk_state.state
            if not args.enable_signals:
                return
            # 调用执行安全状态查询确认至少存在一条已批准策略；仅观察行情时不创建模拟账本。
            safety = read_execution_safety_status(oanda_enabled=False)
            if safety.approved_qualifications > 0:
                # 调用本地模拟账本刷新，在策略获准且信号观察启动时立即固定初始资金。
                # 30天计时不应依赖首次入场信号；此处只建立资金快照，不创建订单或成交。
                paper_account = refresh_paper_account(
                    now=snapshot_time,
                    initial_equity=account.total,
                )
                if paper_account is None:
                    raise RuntimeError("获准策略存在，但Gate模拟资金账本初始化失败")
            # 调用新的15m与5m独立同周期观察；旧EMA和跨周期信号不再进入订单路径。
            signal_summary = run_bollinger_signal_cycle(
                bar_limit_5m=args.bar_limit,
                bar_limit_15m=max(300, args.bar_limit // 3),
            )
            blocked_streams = int(signal_summary.status.startswith("BLOCKED"))
            _runtime_log(
                f"信号观察完成：新增{signal_summary.new_signal_count}条，"
                f"阻断行情流{blocked_streams}条，订单{signal_summary.order_count}张，"
                f"影子权益{signal_summary.paper_equity:.2f} USDT"
            )
            for paper_event in signal_summary.paper_events:
                # 每次模拟开仓、减仓、平仓只推钉钉；邮箱留给系统事件。
                event_notifier.send(
                    event_key=paper_event.event_key,
                    event_title=paper_event.title,
                    event_lines=paper_event.lines,
                    severity=paper_event.severity,
                    repeatable=True,
                    category="TRADE",
                )
            if signal_summary.status != previous_signal_status and signal_summary.status in {
                "BLOCKED_MARKET_HEALTH",
                "BLOCKED_EXECUTION_NOT_READY",
                "BLOCKED_QUALIFICATION",
            }:
                # 状态只有发生变化时才调用一次通知，避免每分钟重复发送相同阻断邮件。
                event_notifier.send(
                    event_key=f"signal-status:{signal_summary.status}",
                    event_title=f"策略状态：{signal_summary.status}",
                    event_lines=(f"原因：{signal_summary.reason}",),
                    severity=(
                        "CRITICAL"
                        if signal_summary.status == "BLOCKED_MARKET_HEALTH"
                        else "WARNING"
                    ),
                )
            previous_signal_status = signal_summary.status
            # 调用成交游标扫描，每一笔新买入、卖出或止损成交各发送一封独立邮件。
            sent_trade_emails = event_notifier.notify_new_trades()
            if sent_trade_emails:
                _runtime_log(f"本轮已发送{sent_trade_emails}封成交邮件")
            # 调用30天监督器汇总完整UTC日；账户未初始化时不会提前启动计时。
            simulation = refresh_paper_simulation() if safety.approved_qualifications > 0 else None
            if simulation is not None:
                _runtime_log(
                    f"模拟监督：{simulation.status}，连续健康"
                    f"{simulation.consecutive_healthy_days}/{simulation.required_days}天"
                )
            else:
                _runtime_log("新策略尚未通过准入，旧EMA模拟监督不再续期")

        # 调用行情运行器；每轮刷新风控和新布林带信号，并旁路发送生命周期事件邮件。
        runner = MarketDataRunner(
            settings,
            runner_config,
            stop_event=stop_event,
            reporter=_runtime_log,
            event_reporter=report_runtime_event,
            after_cycle=refresh_gate_risk_and_signals_after_market_cycle,
        )
        _runtime_log("行情服务启动；仅连接Gate测试网，真实交易始终关闭")
        # 文件锁阻止本机重复进程；信号处理器让Ctrl+C和Docker停止都能安全收尾。
        with SingleInstanceLock(args.lock_file), install_shutdown_signal_handlers(stop_event):
            result = runner.run()
        _runtime_log(f"行情服务已安全停止，成功轮询{result.successful_cycles}轮")
        _runtime_log("Exchange order submission available: False")
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
            _runtime_log(
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
                    capability_client.get_instrument_rules(account_id, args.oanda_instrument)
                    current_price = capability_client.get_price(account_id, args.oanda_instrument)
            except OandaApiError as error:
                _runtime_log(f"黄金模拟执行门禁：BLOCKED_ACCOUNT_INSTRUMENT，原因：{error}")
                return
            if not current_price.tradeable:
                _runtime_log("黄金模拟执行门禁：BLOCKED_NOT_TRADEABLE")
                return
            # 调用黄金独立模拟账本；首次固定Practice净值，之后每轮只刷新本地权益。
            refresh_paper_account(
                now=snapshot_time,
                initial_equity=account.nav,
                paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
            )
            summaries = []
            primary_intervals = tuple(
                interval for interval in args.intervals if interval == args.oanda_paper_interval
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
                interval for interval in args.intervals if interval != args.oanda_paper_interval
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
            blocked_streams = sum(stream.status.startswith("BLOCKED") for stream in streams)
            new_signal_count = sum(summary.new_signal_count for summary in summaries)
            order_count = sum(summary.order_count for summary in summaries)
            _runtime_log(
                f"黄金信号观察完成：新增{new_signal_count}条，"
                f"阻断行情流{blocked_streams}条，本地模拟订单{order_count}张"
            )
            # 调用OANDA专属30天监督器，黄金权益与OANDA服务心跳均不混入Gate记录。
            simulation = refresh_paper_simulation(
                paper_venue=OANDA_PAPER_ACCOUNT_VENUE,
                market_service_name=OANDA_MARKET_DATA_SERVICE_NAME,
            )
            if simulation is not None:
                _runtime_log(
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
            reporter=_runtime_log,
            service_name=OANDA_MARKET_DATA_SERVICE_NAME,
        )
        _runtime_log("OANDA黄金行情服务启动；仅连接Practice，真实交易始终关闭")
        # 调用独立文件锁和信号处理器，确保不与Gate服务争用锁且停止时完整收尾。
        with (
            SingleInstanceLock(args.oanda_lock_file),
            install_shutdown_signal_handlers(oanda_stop_event),
        ):
            result = oanda_runner.run()
        _runtime_log(f"OANDA黄金行情服务已安全停止，成功轮询{result.successful_cycles}轮")
        _runtime_log("Exchange order submission available: False")
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
        # 调用5m到1h优先级轨道轮转影子模拟；旧EMA策略不再生成订单。
        summary = run_bollinger_signal_cycle(
            bar_limit_5m=args.bar_limit,
            bar_limit_15m=max(300, args.bar_limit // 3),
        )
        print(
            f"BTC_USDT/ETH_USDT 多周期共享轨道轮转: status={summary.status}, "
            f"new_signals={summary.new_signal_count}, orders={summary.order_count}"
        )
        print(f"  原因: {summary.reason}")
        print(f"Shadow paper equity: {summary.paper_equity:.2f} USDT")
        print(f"New strategy signals: {summary.new_signal_count}")
        print(f"Paper orders created: {summary.order_count}")
        # 调用30天监督器刷新日度指标；该结论只用于人工复核，不开启真实交易。
        safety = read_execution_safety_status(oanda_enabled=False)
        simulation = refresh_paper_simulation() if safety.approved_qualifications > 0 else None
        if simulation is None:
            print("Paper simulation: NOT_STARTED_FOR_ACTIVE_STRATEGY")
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
