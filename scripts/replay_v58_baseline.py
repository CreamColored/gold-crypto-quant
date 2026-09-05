#!/usr/bin/env python3
"""用 V5.8 策略跑一段历史行情，结果留存为后续改动的对照基线。

和 scripts/replay_structure_rule.py 同一个驱动方式——**逐分钟推进**，
每一步只把当时已经收线的K线交给模拟器。不能一次性喂整段行情：
box_active 是单份可变状态，整段回放会让分钟循环读到收盘后的箱体结论，
等于偷看未来。

与那个脚本的两点不同：
  1. 切片用 searchsorted 定位再 iloc，不是每分钟对全表做布尔掩码。
     一年是 52 万分钟，掩码方式 O(n) 每步，跑不完。
  2. 不喂在途K线。秒级报价只有最近几天，一年的窗口里没有，
     所以这条基线是"按分钟收线触轨"的口径，改动后的对照必须用同一口径。

用法：
    python scripts/replay_v58_baseline.py --days 365
    python scripts/replay_v58_baseline.py --days 30 --venue BINANCE_LIVE_PUBLIC
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import create_engine, text  # noqa: E402

from gold_crypto_quant.config import get_settings  # noqa: E402
from gold_crypto_quant.runtime import multi_timeframe_rotation_simulator as sim  # noqa: E402

MAIN_INTERVALS = ("5m", "15m", "30m", "1h")
WINDOW = 400
OUTPUT_ROOT = Path("var/baseline")
_ENGINE = create_engine(get_settings().database_url, pool_pre_ping=True)


def load_bars(symbol: str, interval: str, venue: str, start, end) -> pd.DataFrame:
    sql = """SELECT b.open_time, b.open_price AS open, b.high_price AS high,
                    b.low_price AS low, b.close_price AS close, b.volume
             FROM market_bars b JOIN instruments i ON i.id = b.instrument_id
             WHERE i.symbol=:s AND b.interval_code=:iv AND i.venue=:v
               AND b.open_time >= :a AND b.open_time < :z
             ORDER BY b.open_time"""
    frame = pd.read_sql(text(sql), _ENGINE,
                        params={"s": symbol, "iv": interval, "v": venue, "a": start, "z": end})
    if frame.empty:
        return frame
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    frame = frame.set_index("open_time")
    # MySQL 的 DATETIME 不带时区，但库里存的语义一直是 UTC；
    # 模拟器内部要按 UTC 比较并转北京时算交易日，必须显式贴上时区。
    frame.index = pd.DatetimeIndex(frame.index).tz_localize("UTC")
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="V5.8 历史回放基线")
    parser.add_argument("--days", type=float, default=365.0)
    parser.add_argument("--venue", default="BINANCE_LIVE_PUBLIC")
    parser.add_argument("--symbols", default="BTC_USDT,ETH_USDT")
    parser.add_argument("--tag", default="v58-baseline")
    parser.add_argument("--progress-every", type=int, default=20000)
    parser.add_argument("--entry-intervals",
                        help="覆盖入场周期，逗号分隔。V5.8 默认 15m,30m,1h——"
                             "5m 被排除在入场之外，只参与顶底结构判断。"
                             "传 5m,15m,30m,1h 就是放开 5m 交易。")
    parser.add_argument("--regime-off", action="store_true",
                        help="关掉 V5.9 的震荡识别，复现 V5.8 的箱体判定，用于 A/B 对照")
    parser.add_argument("--disable-fuse", action="store_true",
                        help="关掉 8%% 最大回撤永久熔断。研究策略本身时要摘掉风控闸门——"
                             "否则量到的是闸门什么时候关，不是策略好不好。线上绝不能关。")
    args = parser.parse_args()

    symbols = tuple(args.symbols.split(","))
    latest = pd.read_sql(
        text("SELECT MAX(open_time) t FROM market_bars b JOIN instruments i "
             "ON i.id=b.instrument_id WHERE i.venue=:v"),
        _ENGINE, params={"v": args.venue})["t"].iloc[0]
    end = pd.Timestamp(latest)
    start = end - timedelta(days=args.days)

    main_bars = {s: {i: load_bars(s, i, args.venue, start, end) for i in MAIN_INTERVALS}
                 for s in symbols}
    micro = {s: load_bars(s, "1m", args.venue, start, end) for s in symbols}
    missing = [f"{s} {i}" for s in symbols for i, b in main_bars[s].items() if b.empty]
    missing += [f"{s} 1m" for s in symbols if micro[s].empty]
    if missing:
        print(f"缺少数据：{missing}", file=sys.stderr)
        return 2

    if args.regime_off:
        from gold_crypto_quant.strategy import bollinger_range as br
        br.REGIME_FILTER_ENABLED = False
    if args.disable_fuse:
        sim.MAX_DRAWDOWN_FUSE = 1.0
    if args.entry_intervals:
        # 三处使用点都在函数内读模块全局，运行前覆盖即可，不必改死代码。
        sim.ENTRY_INTERVAL_PRIORITY = tuple(
            x.strip() for x in args.entry_intervals.split(",") if x.strip()
        )
    print(f"窗口 {start:%Y-%m-%d} → {end:%Y-%m-%d}（{args.days:.0f} 天）  {args.venue}")
    print(f"最大回撤熔断：{'已关闭（仅研究用）' if args.disable_fuse else f'{sim.MAX_DRAWDOWN_FUSE:.0%}'}")
    print(f"入场周期：{'、'.join(sim.ENTRY_INTERVAL_PRIORITY)}")
    from gold_crypto_quant.strategy import bollinger_range as _br
    print(f"震荡识别：{'V5.9 四条判据' if _br.REGIME_FILTER_ENABLED else 'V5.8 仅三轨走平'}")
    for s in symbols:
        counts = "  ".join(f"{i}:{len(b):,}" for i, b in main_bars[s].items())
        print(f"  {s}  1m:{len(micro[s]):,}  {counts}")

    # 收线时间预先算好；每一步用 searchsorted 定位，避免逐分钟全表掩码。
    # searchsorted 只用于定位切点。tz-aware 的 DatetimeIndex 转 numpy 会得到
    # 对象数组，比较时会和 tz-naive 的 stamp 冲突，所以两边一律先剥掉时区，
    # 在同一套 UTC 数值上比。交给模拟器的仍然是带时区的 Timestamp。
    close_times = {
        s: {i: (b.index + sim.INTERVAL_DURATION[i]).tz_localize(None).to_numpy()
            for i, b in d.items()}
        for s, d in main_bars.items()
    }
    micro_close = {
        s: (m.index + pd.Timedelta(minutes=1)).tz_localize(None).to_numpy()
        for s, m in micro.items()
    }

    minutes = micro[symbols[0]].index
    # 预热：等所有序列都攒够模拟器要的根数再开始计入台账。
    warm = max(400, WINDOW)
    minutes = minutes[warm:]
    print(f"  逐分钟推进 {len(minutes):,} 步\n")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    state_path = OUTPUT_ROOT / f"state-{args.tag}.json"
    state_path.unlink(missing_ok=True)

    events: list = []
    equity = 10_000.0
    started = time.monotonic()
    for step, now in enumerate(minutes):
        stamp = now.tz_localize(None).to_numpy()
        main_slice = {}
        for s in symbols:
            per = {}
            for i in MAIN_INTERVALS:
                cut = int(np.searchsorted(close_times[s][i], stamp, side="right"))
                if cut < 30:
                    break
                per[i] = main_bars[s][i].iloc[max(0, cut - WINDOW):cut]
            if len(per) < len(MAIN_INTERVALS):
                main_slice = {}
                break
            main_slice[s] = per
        if not main_slice:
            continue
        micro_slice = {}
        for s in symbols:
            cut = int(np.searchsorted(micro_close[s], stamp, side="right"))
            if cut < 65:
                micro_slice = {}
                break
            micro_slice[s] = micro[s].iloc[max(0, cut - WINDOW):cut]
        if not micro_slice:
            continue

        summary = sim.run_multi_timeframe_paper_cycle(
            main_slice, micro_bars_by_symbol=micro_slice,
            state_path=state_path, now=now.to_pydatetime(),
        )
        events.extend(summary.events)
        equity = summary.equity

        if args.progress_every and step and step % args.progress_every == 0:
            rate = step / (time.monotonic() - started)
            left = (len(minutes) - step) / rate / 60
            print(f"  [{step:>7,}/{len(minutes):,}] 权益 {equity:,.2f}U  "
                  f"成交 {len(events)} 笔  {rate:.0f} 步/秒  剩约 {left:.0f} 分钟")

    elapsed = time.monotonic() - started
    opens = [e for e in events if "开仓" in e.title]
    closes = [e for e in events if "开仓" not in e.title]
    reasons = Counter(e.title for e in closes)
    wins = sum(1 for e in closes if getattr(e, "pnl", 0) and e.pnl > 0)

    print(f"\n完成，耗时 {elapsed/60:.1f} 分钟")
    print(f"  最终权益 {equity:,.2f}U（初始 10,000）  收益 {equity/10_000-1:+.2%}")
    print(f"  开仓 {len(opens)} 笔，平仓事件 {len(closes)} 笔")
    print(f"  平均每天 {len(opens)/args.days:.2f} 笔")
    print("  出场原因：")
    for reason, n in reasons.most_common(10):
        print(f"    {n:>5} 次  {reason}")

    payload = {
        "tag": args.tag, "venue": args.venue, "symbols": list(symbols),
        "window": {"start": str(start), "end": str(end), "days": args.days},
        "strategy_version": sim.MULTI_ROTATION_STRATEGY_VERSION,
        "max_drawdown_fuse": sim.MAX_DRAWDOWN_FUSE,
        "entry_intervals": list(sim.ENTRY_INTERVAL_PRIORITY),
        "regime_filter": _br.REGIME_FILTER_ENABLED,
        "final_equity": equity, "total_return": equity / 10_000 - 1,
        "opens": len(opens), "closes": len(closes), "wins": wins,
        "exit_reasons": dict(reasons),
        "elapsed_minutes": round(elapsed / 60, 2),
        # RotationPaperEvent 的字段是 event_key/title/lines/severity。
        # event_key 形如 rotation-v5:<时间>:<品种>:<周期>:<标题>:<序号>，
        # 明细在 lines 里。上一轮按 moment/detail/pnl 取属性，全取到空值。
        "events": [
            {"key": e.event_key, "title": e.title,
             "lines": list(e.lines), "severity": e.severity}
            for e in events
        ],
    }
    out = OUTPUT_ROOT / f"{args.tag}-{pd.Timestamp.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\n结果已留存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
