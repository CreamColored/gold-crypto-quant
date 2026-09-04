#!/usr/bin/env python3
"""震荡 v1.0 / 顺势 v1.0 的参数扫描。

在 7 天训练窗口上遍历参数组合，再把每组拿到未参与优化的样本外窗口上复核。

为什么一定要留样本外：7 天里两个品种加起来只有二三十笔交易，
在这个样本量上"最优解"和噪声几乎无法区分。训练窗最好的那组，
如果在样本外掉下去，说明它拟合的是这一周的具体走势，不是规律。
脚本会把两个窗口的结果并排打出来，差距自己看。

用法：
    python scripts/sweep_v1.py --strategy range --train-days 7 --holdout-days 3
    python scripts/sweep_v1.py --strategy trend --top 15
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gold_crypto_quant.config import get_settings  # noqa: E402
from gold_crypto_quant.strategies_v1.backtest import run_backtest  # noqa: E402
from gold_crypto_quant.strategies_v1.params import RangeParams, TrendParams  # noqa: E402
from gold_crypto_quant.strategies_v1.range_v1 import RangeStrategyV1  # noqa: E402
from gold_crypto_quant.strategies_v1.trend_v1 import TrendStrategyV1  # noqa: E402

VENUE = "GATE_LIVE_PUBLIC"
INTERVALS = ("15m", "30m", "1h")

RANGE_GRID: dict[str, list] = {
    "touch_tolerance": [0.0010, 0.0015, 0.0025, 0.0040],
    "min_touches": [2, 3],
    "entry_zone": [0.10, 0.15, 0.25],
    "stop_buffer": [0.15, 0.25, 0.40, 0.60],
    "lookback_bars": [100, 200, 300],
    "require_edge_rejection": [True, False],
}
TREND_GRID: dict[str, list] = {
    "min_waves": [2, 3],
    "min_wave_bars": [1, 2],
    "intervals": [("1h", "15m"), ("30m", "15m"), ("1h", "30m")],
    "require_zero_axis": [True, False],
    "require_volume": [True, False],
    "pullback": [(0.20, 0.75), (0.10, 0.90), (0.30, 0.60)],
    "stop_atr_multiple": [0.5, 1.0, 1.5],
}

_BARS: dict = {}


def _engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def load_bars(symbol: str, interval: str, start, end) -> pd.DataFrame:
    sql = """SELECT b.open_time, b.open_price AS open, b.high_price AS high,
                    b.low_price AS low, b.close_price AS close, b.volume
             FROM market_bars b JOIN instruments i ON i.id = b.instrument_id
             WHERE i.symbol=:s AND b.interval_code=:iv AND i.venue=:v
               AND b.open_time >= :a AND b.open_time < :z
             ORDER BY b.open_time"""
    frame = pd.read_sql(
        text(sql), _engine(),
        params={"s": symbol, "iv": interval, "v": VENUE, "a": start, "z": end},
    )
    if frame.empty:
        return frame
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    return frame.set_index("open_time")


@dataclass(frozen=True, slots=True)
class Outcome:
    label: str
    combo: dict
    trades: int
    wins: int
    pnl: float
    drawdown: float
    fees: float
    profit_factor: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


def _build(kind: str, combo: dict):
    """把一组扫描值变成参数对象；返回 (params, 驱动周期)。"""
    if kind == "range":
        return RangeParams().replace(**combo), "15m"
    changes = dict(combo)
    direction, entry = changes.pop("intervals")
    low, high = changes.pop("pullback")
    params = TrendParams().replace(
        direction_interval=direction, entry_interval=entry,
        pullback_min=low, pullback_max=high, **changes,
    )
    return params, entry


def _evaluate(job) -> Outcome:
    kind, combo, symbols, window_key = job
    params, driver = _build(kind, combo)
    factory = RangeStrategyV1 if kind == "range" else TrendStrategyV1
    trades = wins = 0
    pnl = fees = 0.0
    worst = 0.0
    gains = drops = 0.0
    for symbol in symbols:
        bars = _BARS[window_key][symbol]
        if any(len(bars[i]) < 60 for i in INTERVALS):
            continue
        result = run_backtest(
            factory(params), symbol=symbol, bars=bars, driver_interval=driver,
            risk_per_trade=params.risk_per_trade, warmup=60,
        )
        trades += result.trades
        wins += result.wins
        pnl += result.final_equity - result.initial_equity
        fees += result.total_fees
        worst = max(worst, result.max_drawdown)
        gains += sum(e.pnl for e in result.events if e.pnl > 0)
        drops += -sum(e.pnl for e in result.events if e.pnl < 0)
    return Outcome(
        label=window_key, combo=combo, trades=trades, wins=wins, pnl=pnl,
        drawdown=worst, fees=fees,
        profit_factor=(gains / drops) if drops > 0 else (float("inf") if gains else 0.0),
    )


def _init(bars):
    global _BARS
    _BARS = bars


def _combos(grid: dict) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*grid.values())]


def _fmt(outcome: Outcome | None) -> str:
    if outcome is None or outcome.trades == 0:
        return f"{'0笔':>16}"
    return (
        f"{outcome.trades:>3}笔 {outcome.win_rate:>4.0%} "
        f"{outcome.pnl:>+8.1f}U 回撤{outcome.drawdown:>5.1%}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="策略参数扫描")
    parser.add_argument("--strategy", choices=("range", "trend"), required=True)
    parser.add_argument("--symbols", default="BTC_USDT,ETH_USDT")
    parser.add_argument("--train-days", type=float, default=7.0)
    parser.add_argument("--holdout-days", type=float, default=3.0)
    parser.add_argument("--min-trades", type=int, default=8,
                        help="低于这个笔数不参与排名——两三笔的暴利是噪声")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--out", default="var/sweeps")
    args = parser.parse_args()

    symbols = tuple(args.symbols.split(","))
    latest = pd.read_sql(text("SELECT MAX(open_time) AS t FROM market_bars"), _engine())["t"].iloc[0]
    end = pd.Timestamp(latest)
    # 样本外取最近一段，训练窗在它之前——用过去优化、在"未来"上检验，
    # 顺序反过来就等于拿答案去凑题。
    holdout_start = end - timedelta(days=args.holdout_days)
    train_end = holdout_start
    train_start = train_end - timedelta(days=args.train_days)

    windows = {
        "训练": (train_start, train_end),
        "样本外": (holdout_start, end),
    }
    bars = {
        key: {
            symbol: {iv: load_bars(symbol, iv, lo, hi) for iv in INTERVALS}
            for symbol in symbols
        }
        for key, (lo, hi) in windows.items()
    }
    for key, (lo, hi) in windows.items():
        counts = {s: len(bars[key][s]["15m"]) for s in symbols}
        print(f"{key}窗口 {lo} → {hi}   15m根数 {counts}")

    grid = RANGE_GRID if args.strategy == "range" else TREND_GRID
    combos = _combos(grid)
    print(f"\n策略 {args.strategy}：{len(combos)} 组参数 × {len(symbols)} 品种 × 2 窗口")

    jobs = [(args.strategy, c, symbols, w) for w in windows for c in combos]
    with ProcessPoolExecutor(initializer=_init, initargs=(bars,)) as pool:
        results = list(pool.map(_evaluate, jobs, chunksize=8))

    train = {json.dumps(o.combo, sort_keys=True, default=str): o
             for o in results if o.label == "训练"}
    holdout = {json.dumps(o.combo, sort_keys=True, default=str): o
               for o in results if o.label == "样本外"}

    ranked = sorted(
        (o for o in train.values() if o.trades >= args.min_trades),
        key=lambda o: o.pnl, reverse=True,
    )
    if not ranked:
        print(f"\n没有任何一组达到 {args.min_trades} 笔的门槛。降低 --min-trades 再看。")
        best_any = sorted(train.values(), key=lambda o: o.pnl, reverse=True)[:5]
        for o in best_any:
            print(f"  {_fmt(o)}  {o.combo}")
        return 1

    print(f"\n训练窗前 {args.top} 名（≥{args.min_trades} 笔），并列出各自的样本外表现：\n")
    print(f"  {'#':>2}  {'训练窗':<34} {'样本外':<34} 参数")
    for rank, outcome in enumerate(ranked[: args.top], 1):
        key = json.dumps(outcome.combo, sort_keys=True, default=str)
        out = holdout.get(key)
        print(f"  {rank:>2}  {_fmt(outcome):<34} {_fmt(out):<34} {outcome.combo}")

    kept = [o for o in ranked if (h := holdout.get(
        json.dumps(o.combo, sort_keys=True, default=str))) and h.pnl > 0]
    print(f"\n训练窗为正且样本外也为正的：{len(kept)} / {len(ranked)} 组")
    if kept:
        print("  样本外仍为正的最好一组：")
        best = kept[0]
        print(f"    {best.combo}")
        print(f"    训练 {_fmt(best)}   样本外 {_fmt(holdout[json.dumps(best.combo, sort_keys=True, default=str)])}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{args.strategy}-{pd.Timestamp.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(
        [{"window": o.label, "combo": o.combo, "trades": o.trades, "wins": o.wins,
          "pnl": round(o.pnl, 4), "drawdown": round(o.drawdown, 6),
          "fees": round(o.fees, 2), "profit_factor": round(o.profit_factor, 4)}
         for o in results], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n全部结果已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
