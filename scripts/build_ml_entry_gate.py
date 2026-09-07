#!/usr/bin/env python3
"""为某一折生成 ML 入场闸门评分表。

关键的因果约束：第 t 根 15m K 线的分数，只用第 t-1 根收盘为止的特征算。
所以这个分数在第 t 根开盘时就已经确定，第 t 根内任何一分钟触轨都可以用它，
不构成未来函数。

阈值取**训练集**预测分布的分位数，不是测试段的——用测试段分位数
等于让阈值偷看了测试段的分布。
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sklearn.ensemble import HistGradientBoostingRegressor  # noqa: E402

from gold_crypto_quant.storage.database import build_engine  # noqa: E402
from gold_crypto_quant.storage.market_bars import load_market_bars  # noqa: E402
from research_range_ml import BINANCE, build_features  # noqa: E402

DROP = ["bb_upper", "bb_middle", "bb_lower"]


def full_bar_scores(symbol: str, engine, model, columns) -> pd.DataFrame:
    """给每一根 15m K 线算多空两个分数，索引是该K线的 open_time。"""
    bars = load_market_bars(symbol, "15m", engine=engine, venue=BINANCE)
    higher = {
        "m30": load_market_bars(symbol, "30m", engine=engine, venue=BINANCE),
        "h1": load_market_bars(symbol, "1h", engine=engine, venue=BINANCE),
    }
    prev = build_features(bars, higher).shift(1).drop(columns=DROP)
    out = pd.DataFrame(index=bars.index)
    for side, flag in (("LONG", 1), ("SHORT", 0)):
        frame = prev.copy()
        frame["is_long"] = flag
        out[side] = model.predict(frame[columns])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events-dir", required=True, help="events_*.pkl 所在目录")
    ap.add_argument("--train-end", required=True, help="训练数据截止日（不含），YYYY-MM-DD")
    ap.add_argument("--top", type=float, default=0.90, help="保留预测分数最高的比例门槛")
    ap.add_argument("--symbols", nargs="+", default=["ETH_USDT", "BTC_USDT"])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    engine = build_engine()
    payload: dict = {"train_end": args.train_end, "top": args.top, "symbols": {}}
    for symbol in args.symbols:
        df = pd.read_pickle(Path(args.events_dir) / f"events_{symbol}.pkl")
        ts = pd.to_datetime(df["_ts"], utc=True)
        train = (ts < pd.Timestamp(args.train_end, tz="UTC")).to_numpy()
        X = df.drop(columns=["_label", "_net_r", "_ts", "_symbol", "_side"])
        model = HistGradientBoostingRegressor(
            max_iter=400, learning_rate=0.05, max_leaf_nodes=31,
            min_samples_leaf=80, l2_regularization=1.0, random_state=7,
        ).fit(X[train], df["_net_r"].to_numpy()[train])
        # 阈值来自训练集，不看测试段
        threshold = float(np.quantile(model.predict(X[train]), args.top))
        scores = full_bar_scores(symbol, engine, model, list(X.columns))
        payload["symbols"][symbol] = {"scores": scores, "threshold": threshold}
        kept = float((scores.to_numpy() >= threshold).mean())
        print(f"{symbol}: 训练 {int(train.sum())} 笔  阈值 {threshold:+.4f}"
              f"  全体K线通过率 {kept:.1%}  评分 {len(scores)} 根")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as handle:
        pickle.dump(payload, handle)
    print(f"已写出 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
