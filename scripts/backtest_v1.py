#!/usr/bin/env python3
"""震荡 v1.0 与顺势 v1.0 的固定窗口对照回测。

用法：
    python scripts/backtest_v1.py --days 7
    python scripts/backtest_v1.py --days 7 --symbols BTC_USDT
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gold_crypto_quant.config import get_settings  # noqa: E402
from gold_crypto_quant.strategies_v1.backtest import run_backtest  # noqa: E402
from gold_crypto_quant.strategies_v1.params import load_config  # noqa: E402
from gold_crypto_quant.strategies_v1.range_v1 import RangeStrategyV1  # noqa: E402
from gold_crypto_quant.strategies_v1.trend_v1 import TrendStrategyV1  # noqa: E402

INTERVALS = ("15m", "30m", "1h")
_ENGINE = create_engine(get_settings().database_url, pool_pre_ping=True)


def load_bars(symbol: str, interval: str, venue: str, start=None, end=None) -> pd.DataFrame:
    sql = """SELECT b.open_time, b.open_price AS open, b.high_price AS high,
                    b.low_price AS low, b.close_price AS close, b.volume
             FROM market_bars b JOIN instruments i ON i.id = b.instrument_id
             WHERE i.symbol=:s AND b.interval_code=:iv AND i.venue=:v"""
    params: dict = {"s": symbol, "iv": interval, "v": venue}
    if start is not None:
        sql += " AND b.open_time >= :a"
        params["a"] = start
    if end is not None:
        sql += " AND b.open_time < :z"
        params["z"] = end
    frame = pd.read_sql(text(sql + " ORDER BY b.open_time"), _ENGINE, params=params)
    if frame.empty:
        return frame
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    return frame.set_index("open_time")


def main() -> int:
    parser = argparse.ArgumentParser(description="震荡 v1.0 与顺势 v1.0 对照回测")
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--symbols", default="BTC_USDT,ETH_USDT")
    parser.add_argument("--venue", default="GATE_LIVE_PUBLIC")
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--warmup", type=int, default=120)
    args = parser.parse_args()

    latest = pd.read_sql(
        text("SELECT MAX(open_time) AS t FROM market_bars"), _ENGINE
    )["t"].iloc[0]
    end = pd.Timestamp(latest)
    start = end - timedelta(days=args.days)
    config = load_config()
    print(f"窗口：{start} → {end}（{args.days} 天）  交易所 {args.venue}")
    print(f"参数：config/strategies-v1.json\n")

    rows = []
    for symbol in args.symbols.split(","):
        bars = {
            interval: load_bars(symbol, interval, args.venue, start, end)
            for interval in INTERVALS
        }
        missing = [k for k, v in bars.items() if v.empty]
        if missing:
            print(f"  {symbol} 缺少周期 {missing}，跳过")
            continue
        counts = "  ".join(f"{k}:{len(v)}根" for k, v in bars.items())
        print(f"  {symbol}  {counts}")
        for strategy, risk in (
            (RangeStrategyV1(config.range_params), config.range_params.risk_per_trade),
            (TrendStrategyV1(config.trend_params), config.trend_params.risk_per_trade),
        ):
            result = run_backtest(
                strategy, symbol=symbol, bars=bars, driver_interval="15m",
                risk_per_trade=risk, initial_equity=args.equity, warmup=args.warmup,
            )
            rows.append(result)
            print(f"    {result.summary_line()}")
    print()
    if rows:
        print("汇总：")
        for name in ("震荡", "顺势"):
            group = [r for r in rows if r.strategy == name]
            if not group:
                continue
            trades = sum(r.trades for r in group)
            pnl = sum(r.final_equity - r.initial_equity for r in group)
            wins = sum(r.wins for r in group)
            print(
                f"  {name} v1.0：{trades} 笔，"
                f"胜率 {wins / trades:.0%}" if trades else f"  {name} v1.0：0 笔",
                end="",
            )
            print(f"，合计盈亏 {pnl:+.2f}U" if trades else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
