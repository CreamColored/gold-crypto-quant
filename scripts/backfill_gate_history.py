#!/usr/bin/env python3
"""回补 Gate 实盘公共行情的历史K线。

按 2000 根一页从近往远翻，写入 GATE_LIVE_PUBLIC。幂等——同一根重复写会被
ON DUPLICATE KEY 挡掉，所以中断之后直接重跑即可，不必算断点。

只拉 5m 及以上：新策略的评估时点是已收线K线，最小驱动周期 15m，
1m 是给 V5.8 的在途触轨用的，半年 1m 有 52 万行且这里用不到。

用法：
    python scripts/backfill_gate_history.py --days 180
    python scripts/backfill_gate_history.py --days 180 --intervals 15m,1h --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy.orm import Session  # noqa: E402

from gold_crypto_quant.exchanges.gate import INTERVAL_SECONDS, GatePublicClient  # noqa: E402
from gold_crypto_quant.market_data.gate_history import (  # noqa: E402
    GATE_LIVE_VENUE,
    _ensure_instrument,
    _store_frame,
)
from gold_crypto_quant.storage.database import build_engine  # noqa: E402

PAGE_POINTS = 2000


def main() -> int:
    parser = argparse.ArgumentParser(description="回补 Gate 历史K线")
    parser.add_argument("--days", type=float, default=180.0)
    parser.add_argument("--symbols", default="BTC_USDT,ETH_USDT")
    parser.add_argument("--intervals", default="5m,15m,30m,1h")
    parser.add_argument("--sleep", type=float, default=0.15,
                        help="每页之间的间隔，避免打满公共接口的频率限制")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    symbols = tuple(args.symbols.split(","))
    intervals = tuple(args.intervals.split(","))
    now = datetime.now(UTC)
    floor = now - timedelta(days=args.days)

    plan = []
    for symbol in symbols:
        for interval in intervals:
            span = INTERVAL_SECONDS[interval]
            total = int(args.days * 86400 // span) + 1
            pages = -(-total // PAGE_POINTS)
            plan.append((symbol, interval, total, pages))
    total_pages = sum(p for _, _, _, p in plan)
    print(f"回补窗口：{floor:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M}（{args.days} 天）")
    for symbol, interval, total, pages in plan:
        print(f"  {symbol:<10} {interval:<4} 约 {total:>7,} 根 / {pages:>3} 页")
    print(f"  合计 {total_pages} 次请求，预计 {total_pages * 1.5 / 60:.1f} 分钟\n")
    if args.dry_run:
        return 0

    engine = build_engine()
    grand_fetched = grand_stored = 0
    started = time.monotonic()
    with GatePublicClient() as client:
        for symbol, interval, _, pages in plan:
            span = INTERVAL_SECONDS[interval]
            with Session(engine) as session, session.begin():
                instrument = _ensure_instrument(session, symbol, venue=GATE_LIVE_VENUE)
                instrument_id = instrument.id
            cursor = now
            fetched = stored = 0
            for page in range(pages):
                start = max(floor, cursor - timedelta(seconds=span * (PAGE_POINTS - 1)))
                if start >= cursor:
                    break
                try:
                    frame = client.get_candlesticks(
                        symbol, interval, start_time=start, end_time=cursor
                    )
                except Exception as error:  # noqa: BLE001 - 单页失败不该中止整轮
                    print(f"    {symbol} {interval} 第{page + 1}页失败：{error}")
                    cursor = start
                    continue
                if frame.empty:
                    cursor = start
                    continue
                with Session(engine) as session, session.begin():
                    written, _skipped = _store_frame(
                        session, frame, instrument_id=instrument_id,
                        interval=interval, now=datetime.now(UTC),
                    )
                fetched += len(frame)
                stored += written
                cursor = start
                if cursor <= floor:
                    break
                time.sleep(args.sleep)
            grand_fetched += fetched
            grand_stored += stored
            print(f"  {symbol:<10} {interval:<4} 拉取 {fetched:>7,} 根，新增 {stored:>7,} 行")
    elapsed = time.monotonic() - started
    print(f"\n完成：拉取 {grand_fetched:,} 根，新增 {grand_stored:,} 行，耗时 {elapsed / 60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
