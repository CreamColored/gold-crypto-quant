"""顶底结构规则的对照复盘：同一段行情分别在关闭/开启结构规则下跑影子模拟器。

用法：
    .venv/bin/python scripts/replay_structure_rule.py 2026-09-02
    .venv/bin/python scripts/replay_structure_rule.py 2026-09-03 --since 09:00 --arm structure

--since 之前的行情仍然要跑，只是不计入台账：箱体确认是逐根K线累积的状态，
冷启动直接从关注时刻开跑会让前几笔单子因为箱体尚未确认而消失。

必须逐分钟驱动而不是一次性喂整段行情：box_active 是单份可变状态，
一次性回放会让分钟循环读到收盘后的箱体结论，等于偷看未来。

结果写入 var/replay/<日期>.json，控制台打印两组的交易台账。
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gold_crypto_quant.runtime import multi_timeframe_rotation_simulator as sim  # noqa: E402
from gold_crypto_quant.runtime.provisional_bars import (  # noqa: E402
    load_second_quotes,
    replay_progression,
)
from gold_crypto_quant.storage.market_bars import load_market_bars  # noqa: E402

VENUE = "GATE_LIVE_PUBLIC"
SYMBOLS = ("BTC_USDT", "ETH_USDT", "XAU_USDT")
MAIN_INTERVALS = ("5m", "15m", "30m", "1h")
OUTPUT_ROOT = Path("var/replay")
# 单轮只保留最近若干根，既够MACD和布林带收敛，也避免每分钟重算整年历史。
WINDOW = 400
# --since 之前默认多跑这么多小时，让箱体、止损冷却和MACD都进入稳定状态。
DEFAULT_WARMUP_HOURS = 12


def load_bars(end: pd.Timestamp):
    """读取截止到end的行情；主周期四档加1分钟触轨序列。"""
    main = {
        symbol: {
            interval: load_market_bars(symbol, interval, limit=3000, venue=VENUE)
            for interval in MAIN_INTERVALS
        }
        for symbol in SYMBOLS
    }
    micro = {symbol: load_market_bars(symbol, "1m", limit=3000, venue=VENUE) for symbol in SYMBOLS}
    main = {s: {i: b.loc[b.index < end] for i, b in d.items()} for s, d in main.items()}
    micro = {s: m.loc[m.index < end] for s, m in micro.items()}
    return main, micro


def run(
    tag, minutes, main, micro, state_dir, *,
    disable_structure, reward_risk=0.0, seconds=None, dwell=3.0, second_step=1,
):
    """逐分钟调用模拟器；disable_structure为真时把结构判定整体短路成False。

    传入 ``seconds`` 时额外重放在途K线：每一分钟内按秒推进，形态与实盘写 Redis 的
    完全一致（累计最高最低 + 当秒中间价）。不重放的话，回测代表不了实盘——策略
    现在会在分钟内动作，而只喂收线K线的回放看不到这些。
    """
    original = (sim._has_top_structure, sim._has_bottom_structure)
    if disable_structure:
        sim._has_top_structure = lambda *_a, **_k: False
        sim._has_bottom_structure = lambda *_a, **_k: False
    state_path = state_dir / f"state-{tag}.json"
    state_path.unlink(missing_ok=True)
    events, equity = [], 10_000.0
    try:
        for index, now in enumerate(minutes):
            # 与线上一致：此刻只能看到已经收盘的K线。
            main_slice = {
                s: {
                    i: b.loc[b.index + sim.INTERVAL_DURATION[i] <= now].tail(WINDOW)
                    for i, b in d.items()
                }
                for s, d in main.items()
            }
            micro_slice = {
                s: m.loc[m.index + pd.Timedelta(minutes=1) <= now].tail(WINDOW)
                for s, m in micro.items()
            }
            if any(len(b) < 30 for d in main_slice.values() for b in d.values()):
                continue
            if any(len(m) < 65 for m in micro_slice.values()):
                continue
            summary = sim.run_multi_timeframe_paper_cycle(
                main_slice, micro_bars_by_symbol=micro_slice, state_path=state_path,
                minimum_reward_risk=reward_risk, now=now.to_pydatetime(),
            )
            events.extend(summary.events)
            equity = summary.equity
            if seconds is not None:
                # 收线K线处理完之后，在这一分钟内按秒推进在途K线，与实盘同序。
                progression = {
                    symbol: replay_progression(seconds[symbol], now.to_pydatetime())
                    for symbol in micro_slice
                }
                depth = max((len(v) for v in progression.values()), default=0)
                for tick in range(0, depth, second_step):
                    provisional, moment = {}, None
                    for symbol, steps in progression.items():
                        if tick < len(steps):
                            at, payload = steps[tick]
                            moment = at
                            provisional[symbol] = pd.Series(payload, name=pd.Timestamp(now))
                    if not provisional:
                        continue
                    summary = sim.run_multi_timeframe_paper_cycle(
                        main_slice, micro_bars_by_symbol=micro_slice, state_path=state_path,
                        minimum_reward_risk=reward_risk,
                        provisional_by_symbol=provisional,
                        provisional_dwell_seconds=dwell,
                        now=moment,
                    )
                    events.extend(summary.events)
                    equity = summary.equity
            if index % 200 == 0:
                print(f"  {tag} {now} 权益 {equity:.2f} 事件 {len(events)}", flush=True)
    finally:
        sim._has_top_structure, sim._has_bottom_structure = original
    return [
        {"title": item.title, "lines": list(item.lines), "severity": item.severity}
        for item in events
    ], equity


def _field(lines, key):
    for line in lines:
        if line.startswith(key):
            return line.split("：", 1)[1]
    return ""


def ledger(events):
    """把开仓、减仓、平仓事件还原成一笔一笔的交易。"""
    trades, open_trades = [], {}
    for event in events:
        symbol = _field(event["lines"], "品种")
        moment = _field(event["lines"], "北京时间")[:16]
        if "模拟开仓" in event["title"]:
            trade = {
                "开仓": moment,
                "品种": symbol,
                "周期": _field(event["lines"], "交易周期"),
                "方向": "空" if "做空" in event["title"] else "多",
                "结构": "有" if "已确认" in " ".join(event["lines"]) else "无",
                "盈亏": 0.0,
                "结束": "",
                "原因": "",
            }
            open_trades[symbol] = trade
            trades.append(trade)
        elif symbol in open_trades:
            trade = open_trades[symbol]
            value = _field(event["lines"], "本次净盈亏")
            if value:
                trade["盈亏"] += float(value.split()[0])
            trade["结束"] = moment
            trade["原因"] = event["title"].replace("模拟平仓：", "")
            # 减仓不结束这笔交易，只有平仓才把它从在场仓位里摘掉。
            if "减仓" not in event["title"]:
                open_trades.pop(symbol, None)
    return trades


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="顶底结构规则对照复盘")
    parser.add_argument("day", help="回放截止日期，格式 YYYY-MM-DD（含当天整个UTC自然日）")
    parser.add_argument(
        "--since",
        help="只统计该北京时间时刻之后开的仓，格式 HH:MM（当天）或 'YYYY-MM-DD HH:MM'（可跨日）",
    )
    parser.add_argument(
        "--warmup-hours",
        type=float,
        default=DEFAULT_WARMUP_HOURS,
        help=f"--since 之前额外跑多少小时预热，默认{DEFAULT_WARMUP_HOURS}",
    )
    parser.add_argument(
        "--reward-risk",
        type=float,
        default=0.0,
        help="开仓赔率门槛：到中轨距离 ÷ 止损距离 低于该值就不开单，0=不启用",
    )
    parser.add_argument(
        "--provisional",
        action="store_true",
        help="重放在途K线（逐秒），只对 2026-09-03 15:05 之后的窗口有效",
    )
    parser.add_argument(
        "--dwell", type=float, default=3.0, help="触轨停留确认秒数，0=关闭"
    )
    parser.add_argument(
        "--second-step",
        type=int,
        default=1,
        help="每隔几秒评估一次；大于1会让停留确认失真，仅供快速摸底",
    )
    parser.add_argument(
        "--arm",
        choices=("both", "structure", "baseline"),
        default="both",
        help="跑哪一组：both=两组对照，structure=只跑当前策略",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    day = args.day
    end = pd.Timestamp(day, tz="UTC") + pd.Timedelta(days=1)
    main_bars, micro_bars = load_bars(end)

    # 起点取所有品种都攒够65根1分钟K线的时刻；XAU上线晚于BTC和ETH。
    start = max(m.index[64] for m in micro_bars.values()) + pd.Timedelta(minutes=1)
    cutoff = None
    if args.since:
        # --since 允许带日期，用来统计跨日的时间窗；只给 HH:MM 时默认是截止日当天。
        moment = args.since if " " in args.since else f"{day} {args.since}"
        cutoff = pd.Timestamp(moment, tz="Asia/Shanghai").tz_convert("UTC")
        start = max(start, cutoff - pd.Timedelta(hours=args.warmup_hours))
    minutes = [t for t in micro_bars["ETH_USDT"].index if t >= start]
    if not minutes:
        print(f"{day} 没有可回放的1分钟行情")
        return 1
    print(f"回放 {minutes[0]} → {minutes[-1]}，共 {len(minutes)} 分钟")
    if cutoff is not None:
        print(f"台账只统计 {cutoff.tz_convert('Asia/Shanghai'):%Y-%m-%d %H:%M} 北京时间之后开的仓")

    suffix = f"-rr{args.reward_risk:g}" if args.reward_risk else ""
    target = OUTPUT_ROOT / f"{day}{suffix}"
    target.mkdir(parents=True, exist_ok=True)
    arms = {
        "both": (("baseline", True), ("structure", False)),
        "structure": (("structure", False),),
        "baseline": (("baseline", True),),
    }[args.arm]
    seconds = None
    if args.provisional:
        window_start = minutes[0].to_pydatetime()
        window_end = minutes[-1].to_pydatetime() + pd.Timedelta(minutes=1).to_pytimedelta()
        seconds = {
            symbol: load_second_quotes(VENUE, symbol, window_start, window_end)
            for symbol in SYMBOLS
        }
        covered = {symbol: len(frame) for symbol, frame in seconds.items()}
        print(f"  在途重放：秒级盘口 {covered}", flush=True)
        if not any(covered.values()):
            print("  窗口内没有秒级盘口，退回只喂收线K线", flush=True)
            seconds = None

    results = {}
    for tag, disabled in arms:
        events, equity = run(
            tag, minutes, main_bars, micro_bars, target,
            disable_structure=disabled, reward_risk=args.reward_risk,
            seconds=seconds, dwell=args.dwell, second_step=max(1, args.second_step),
        )
        trades = ledger(events)
        opening_equity = 10_000.0
        if cutoff is not None:
            beijing_cutoff = f"{cutoff.tz_convert('Asia/Shanghai'):%Y-%m-%d %H:%M}"
            # 窗口起点的权益＝窗口内第一笔交易之前的最后一次记录，用来算窗口净盈亏。
            for event in events:
                if _field(event["lines"], "北京时间")[:16] >= beijing_cutoff:
                    break
                snapshot = _field(event["lines"], "影子权益")
                if snapshot:
                    opening_equity = float(snapshot.split()[0])
            trades = [t for t in trades if t["开仓"] >= beijing_cutoff]
        results[tag] = {
            "equity": equity,
            "opening_equity": opening_equity,
            "events": events,
            "trades": trades,
        }
        # 每跑完一组就落盘，打印环节出错也不会白跑一遍。
        (target / "result.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    for tag, data in results.items():
        trades = data["trades"]
        wins = [t for t in trades if t["盈亏"] > 0]
        losses = [t for t in trades if t["盈亏"] < 0]
        decided = len(wins) + len(losses)
        rate = f"{len(wins) / decided:.0%}" if decided else "—"
        label = "旧策略（关闭结构规则）" if tag == "baseline" else "新策略（开启结构规则）"
        opening = data["opening_equity"]
        print(
            f"\n===== {label} =====  窗口起点 {opening:.2f} → 收尾 {data['equity']:.2f}  "
            f"窗口净盈亏 {data['equity'] - opening:+.2f}U  "
            f"{len(trades)}笔  盈{len(wins)} 亏{len(losses)}  胜率 {rate}"
        )
        for trade in trades:
            print(
                f"  {trade['开仓']} {trade['品种']:9s} {trade['周期']:3s} {trade['方向']} "
                f"结构{trade['结构']} → {trade['结束'][11:]:6s} {trade['原因']:22s} "
                f"{trade['盈亏']:+8.2f}U"
            )
    print(f"\n明细已写入 {target / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
