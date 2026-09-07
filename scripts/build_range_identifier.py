#!/usr/bin/env python3
"""学一个真正的震荡识别器，用来**整体替换** box_candidate。

和 E15 的入场闸门不同：那个是在入场处再加一层过滤，这个是替换识别层本身。

标签——"什么是震荡行情"的操作定义：
    在第 t 根冻结上/中/下轨，看之后 N 根K线，同时满足
      1) 存活：没有任何一根收盘越过 上轨+缓冲 或 下轨-缓冲
      2) 双边触达：最高价碰到过上轨，最低价也碰到过下轨
    才算 t 处于震荡行情。

这两条正是策略需要的东西——箱体守得住、而且价格真的在里面来回跑，
也正是"上轨吃下轨、下轨吃上轨"这句话的可计算版本。
标签允许看未来（标签本来就该看未来），**特征一律只用第 t 根收盘为止的数据**。

阈值不是拍的：取训练集上让模型标记比例**等于 V6 现有规则标记比例**的那个分位。
这样 A/B 隔离的是"换哪一批箱体"，而不是"箱体多了还是少了"。
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

from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.metrics import precision_score, recall_score, roc_auc_score  # noqa: E402

from gold_crypto_quant.runtime.multi_timeframe_rotation_simulator import (  # noqa: E402
    _parameters_for_symbol,
)
from gold_crypto_quant.storage.database import build_engine  # noqa: E402
from gold_crypto_quant.storage.market_bars import load_market_bars  # noqa: E402
from gold_crypto_quant.strategy.bollinger_range import (  # noqa: E402
    build_rotation_box_context,
)
from research_range_ml import BINANCE, build_features  # noqa: E402

BAND_COLS = ["bb_upper", "bb_middle", "bb_lower"]


def _forward(series: pd.Series, horizon: int, how: str) -> pd.Series:
    """取第 t+1 到 t+N 根的极值。倒序滚动等价于正序前瞻，避免逐行循环。"""
    shifted = series.shift(-1)[::-1]
    rolled = getattr(shifted.rolling(horizon, min_periods=horizon), how)()
    return rolled[::-1]


def build_labels(bars: pd.DataFrame, feat: pd.DataFrame, buffer: float, horizon: int):
    """震荡行情的操作定义：箱体存活 且 上下两边都被走到。"""
    upper, lower = feat["bb_upper"], feat["bb_lower"]
    high, low, close = bars["high"].astype(float), bars["low"].astype(float), bars["close"].astype(float)

    survived = (
        _forward(close, horizon, "max") <= upper + buffer
    ) & (_forward(close, horizon, "min") >= lower - buffer)
    traversed = (_forward(high, horizon, "max") >= upper) & (
        _forward(low, horizon, "min") <= lower
    )
    return (survived & traversed), survived, traversed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["ETH_USDT"])
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--train-end", required=True)
    ap.add_argument("--horizon", type=int, default=32, help="前瞻K线数，默认32根（15m即8小时）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    engine = build_engine()
    payload: dict = {"train_end": args.train_end, "horizon": args.horizon, "tables": {}}

    for symbol in args.symbols:
        bars = load_market_bars(symbol, args.interval, engine=engine, venue=BINANCE)
        higher = {
            "m30": load_market_bars(symbol, "30m", engine=engine, venue=BINANCE),
            "h1": load_market_bars(symbol, "1h", engine=engine, venue=BINANCE),
        }
        feat = build_features(bars, higher)
        params = _parameters_for_symbol(symbol, args.interval, bars)
        label, survived, traversed = build_labels(
            bars, feat, params.maximum_band_drift, args.horizon
        )

        # V6 现有规则的标记结果，用来对齐比例并做识别质量对照
        rules = build_rotation_box_context(bars, params)["box_candidate"].astype(bool)

        X = feat.drop(columns=BAND_COLS)
        train = (bars.index < pd.Timestamp(args.train_end, tz=bars.index.tz)) & label.notna()
        valid = label.notna() & X.notna().all(axis=1)
        train = train & valid
        y = label.fillna(False).astype(int)

        model = HistGradientBoostingClassifier(
            max_iter=400, learning_rate=0.05, max_leaf_nodes=31,
            min_samples_leaf=80, l2_regularization=1.0, random_state=7,
        ).fit(X[train], y[train])

        proba = pd.Series(model.predict_proba(X.fillna(0.0))[:, 1], index=bars.index)
        # 阈值：让训练集上模型的标记比例 == V6 规则的标记比例
        rule_rate = float(rules[train].mean())
        threshold = float(np.quantile(proba[train], 1.0 - rule_rate))
        flag = (proba >= threshold) & valid

        te = (~train) & valid
        print(f"\n{symbol} {args.interval}  前瞻{args.horizon}根  训练至 {args.train_end}")
        print(f"  震荡行情占比（标签）：训练 {y[train].mean():.1%}   之后 {y[te].mean():.1%}")
        print(f"  V6规则标记率 {rule_rate:.1%}  →  模型阈值 {threshold:.4f}")
        print(f"  {'':<10}{'AUC':>8}{'精确率':>9}{'召回率':>9}{'标记率':>9}")
        for name, pred in (("V6规则", rules[te]), ("学习识别器", flag[te])):
            auc = roc_auc_score(y[te], proba[te]) if name != "V6规则" else roc_auc_score(y[te], rules[te])
            print(f"  {name:<10}{auc:>8.4f}{precision_score(y[te], pred, zero_division=0):>9.1%}"
                  f"{recall_score(y[te], pred, zero_division=0):>9.1%}{pred.mean():>9.1%}")

        payload["tables"][(symbol, args.interval)] = flag

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as handle:
        pickle.dump(payload, handle)
    print(f"\n已写出 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
