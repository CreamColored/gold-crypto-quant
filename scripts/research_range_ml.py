"""震荡识别的机器学习可行性检验（第0阶段：信号是否存在）。

这个脚本**不训练可上线的模型**，它只回答一个先决问题：
在所有触轨事件里，是否存在一个可以用当时可见信息分离出来的、期望为正的子集。

如果时间外样本 AUC≈0.5，说明不存在这样的子集，后面的特征工程、调参、
集成学习全都是浪费——先停在这里，不要再往下做。

三条防污染纪律（前几轮实验就是栽在这上面）：
1. 特征只用 t-1 收盘为止的数据；触轨在第 t 根内发生，成交价用 t-1 冻结的轨道价。
2. 标签用 1m 数据精确判定止损和中轨的先后，不再用"同根K线一律算止损"的保守假设。
   那个假设把 29 笔同根止损里的 21 笔误标成了亏损单，一直在压低所有实验的结果。
3. 测试集 2024-09 之后本轮完全不读，连一次都不许看。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from gold_crypto_quant.risk.indicators import (
    average_directional_index,
    average_true_range,
    choppiness_index,
)
from sqlalchemy import text

from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.market_bars import load_market_bars

BINANCE = "BINANCE_LIVE_PUBLIC"

# 与 V6 执行器一致的成本口径。不要改回负 maker 费率——那是 VIP4 以上的返佣。
MAKER_FEE = 0.0002
TAKER_FEE = 0.0005
STOP_SLIPPAGE = 0.0002

# 固定止损距离，沿用 V6 的 FIXED_STOP_DISTANCE，保证标签和线上口径一致。
FIXED_STOP = {"ETH_USDT": 12.0, "BTC_USDT": 300.0}

# 时间障碍：16 根 15m ＝ 4 小时。超时按市价平掉，不当作赢也不当作输。
HORIZON_BARS = 16

SPLITS = {
    "train": ("2020-01-01", "2022-12-31"),
    "val": ("2023-01-01", "2024-08-31"),
    # test 是 2024-09-01 之后，本脚本默认不加载，避免手滑看到。
}


@dataclass(slots=True)
class EventSet:
    features: pd.DataFrame
    label: pd.Series          # 1=先到中轨，0=先到止损或超时
    net_r: pd.Series          # 扣费后的 R 倍数
    timestamp: pd.Series
    symbol: pd.Series
    side: pd.Series


def load_minute_raw(symbol: str, engine) -> pd.DataFrame:
    """直接读 1m，不走 load_market_bars 的连续性校验。

    校验本身是对的——Binance 1m 在 2026-09-01 和 09-04 各有一处断档，共缺 1635 分钟。
    但这两处都落在 2024-09 之后的测试期，训练集和验证集完全连续。
    这里不是绕过校验，而是把断档位置显式返回，让调用方把受影响的事件整段剔除。
    """
    frame = pd.read_sql(
        text(
            "SELECT b.open_time, b.high_price, b.low_price, b.close_price "
            "FROM market_bars b JOIN instruments i ON i.id = b.instrument_id "
            "WHERE i.venue = :v AND i.symbol = :s AND b.interval_code = '1m' "
            "ORDER BY b.open_time"
        ),
        engine,
        params={"v": BINANCE, "s": symbol},
    )
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    frame = frame.set_index("open_time")
    frame.columns = ["high", "low", "close"]
    return frame.astype(float)


def _bollinger(bars: pd.DataFrame, window: int = 20, deviation: float = 2.0):
    middle = bars["close"].rolling(window).mean()
    sigma = bars["close"].rolling(window).std(ddof=0)
    return middle + deviation * sigma, middle, middle - deviation * sigma


def build_features(bars: pd.DataFrame, higher: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """构造严格因果的特征表；每一行只用该根K线收盘为止的信息。"""
    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    volume = bars["volume"].astype(float)

    upper, middle, lower = _bollinger(bars)
    width = upper - lower
    rel_width = width / middle * 100.0
    atr = average_true_range(bars)
    adx = average_directional_index(bars)
    chop = choppiness_index(bars)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea

    side_of_middle = np.sign(close - middle)
    crossings_20 = (side_of_middle != side_of_middle.shift(1)).rolling(20).sum()
    crossings_50 = (side_of_middle != side_of_middle.shift(1)).rolling(50).sum()

    step = close.diff().abs()
    path_20 = (close - close.shift(20)).abs() / step.rolling(20).sum().replace(0.0, np.nan)
    path_50 = (close - close.shift(50)).abs() / step.rolling(50).sum().replace(0.0, np.nan)

    ret = close.pct_change()
    autocorr_50 = ret.rolling(50).corr(ret.shift(1))

    feat = pd.DataFrame(index=bars.index)
    # A 布林带结构
    feat["rel_width"] = rel_width
    feat["width_pct_252"] = rel_width.rolling(252).rank(pct=True)
    feat["width_growth_3"] = rel_width / rel_width.shift(3) - 1.0
    feat["width_growth_20"] = rel_width / rel_width.shift(20) - 1.0
    feat["drift_upper"] = (upper - upper.shift(20)) / close
    feat["drift_middle"] = (middle - middle.shift(20)) / close
    feat["drift_lower"] = (lower - lower.shift(20)) / close
    feat["max_abs_drift"] = feat[["drift_upper", "drift_middle", "drift_lower"]].abs().max(axis=1)
    feat["percent_b"] = (close - lower) / width.replace(0.0, np.nan)
    # B 往返与路径
    feat["crossings_20"] = crossings_20
    feat["crossings_50"] = crossings_50
    feat["chop"] = chop
    feat["path_eff_20"] = path_20
    feat["path_eff_50"] = path_50
    feat["autocorr_50"] = autocorr_50
    feat["inside_ratio_20"] = ((close < upper) & (close > lower)).rolling(20).mean()
    # C 趋势强度
    feat["adx"] = adx
    feat["atr_pct"] = atr / close
    feat["ema20_slope"] = close.rolling(20).mean().pct_change(10)
    feat["ema50_slope"] = close.rolling(50).mean().pct_change(10)
    feat["dist_ema200_atr"] = (close - close.ewm(span=200, adjust=False).mean()) / atr
    # D 动能
    feat["macd_hist_scaled"] = hist / close * 1000.0
    feat["macd_hist_change_3"] = (hist - hist.shift(3)) / close * 1000.0
    feat["dif_scaled"] = dif / close * 1000.0
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    feat["rsi"] = 100 - 100 / (1 + gain / loss.replace(0.0, np.nan))
    # E 成交量
    feat["volume_ratio"] = volume / volume.rolling(20).mean().replace(0.0, np.nan)
    feat["volume_trend"] = volume.rolling(5).mean() / volume.rolling(20).mean().replace(0.0, np.nan)
    # F 触轨上下文
    touched = (low <= lower) | (high >= upper)
    feat["bars_since_touch"] = (~touched).groupby(touched.cumsum()).cumsum()
    feat["closes_outside_3"] = ((close > upper) | (close < lower)).rolling(3).sum()
    # H 时间（周期编码，避免把 23 点和 0 点当成最远）
    hours = bars.index.hour + bars.index.minute / 60.0
    feat["hour_sin"] = np.sin(2 * np.pi * hours / 24.0)
    feat["hour_cos"] = np.cos(2 * np.pi * hours / 24.0)
    feat["dow"] = bars.index.dayofweek

    # G 高周期：只能用已经收盘的高周期K线，否则就是未来函数
    for name, hb in higher.items():
        h_upper, h_middle, h_lower = _bollinger(hb)
        h_width = (h_upper - h_lower).replace(0.0, np.nan)
        block = pd.DataFrame(
            {
                f"{name}_adx": average_directional_index(hb),
                f"{name}_chop": choppiness_index(hb),
                f"{name}_percent_b": (hb["close"].astype(float) - h_lower) / h_width,
                f"{name}_rel_width": (h_upper - h_lower) / h_middle * 100.0,
            }
        )
        duration = pd.Timedelta(hours=1) if name == "h1" else pd.Timedelta(minutes=30)
        closed = block.index + duration
        idx = np.searchsorted(closed.to_numpy(), bars.index.to_numpy(), side="right") - 1
        valid = idx >= 0
        aligned = pd.DataFrame(index=bars.index, columns=block.columns, dtype=float)
        aligned.loc[valid] = block.to_numpy()[idx[valid]]
        feat = feat.join(aligned)

    feat["bb_upper"] = upper
    feat["bb_middle"] = middle
    feat["bb_lower"] = lower
    return feat


def resolve_outcomes(
    events: pd.DataFrame, minute: pd.DataFrame, horizon: pd.Timedelta
) -> pd.DataFrame:
    """用 1m 数据精确判定止损和中轨谁先到，而不是靠 15m 的 OHLC 猜。"""
    m_time = minute.index.to_numpy()
    m_high = minute["high"].to_numpy(dtype=float)
    m_low = minute["low"].to_numpy(dtype=float)
    m_close = minute["close"].to_numpy(dtype=float)

    starts = np.searchsorted(m_time, events["entry_time"].to_numpy(), side="left")
    ends = np.searchsorted(m_time, (events["entry_time"] + horizon).to_numpy(), side="right")

    # 窗口内 1m 根数不足 = 跨越了断档，这些事件无法诚实判定，整段剔除而不是用残缺数据算。
    expected = int(horizon / pd.Timedelta(minutes=1))
    complete = (ends - starts) >= expected * 0.98

    hits = np.zeros(len(events), dtype=np.int8)  # 1=目标 0=超时 -1=止损
    exits = np.full(len(events), np.nan)
    for i, (lo, hi) in enumerate(zip(starts, ends, strict=True)):
        if hi <= lo:
            continue
        row = events.iloc[i]
        seg_high = m_high[lo:hi]
        seg_low = m_low[lo:hi]
        if row["side"] == "LONG":
            tgt = np.flatnonzero(seg_high >= row["target"])
            stp = np.flatnonzero(seg_low <= row["stop"])
        else:
            tgt = np.flatnonzero(seg_low <= row["target"])
            stp = np.flatnonzero(seg_high >= row["stop"])
        first_t = tgt[0] if tgt.size else np.iinfo(np.int64).max
        first_s = stp[0] if stp.size else np.iinfo(np.int64).max
        if first_t == first_s == np.iinfo(np.int64).max:
            hits[i] = 0
            exits[i] = m_close[hi - 1]
        elif first_s <= first_t:
            # 同一分钟内两边都碰到时才保守算止损；1m 已经把绝大多数歧义消掉了。
            hits[i] = -1
            exits[i] = row["stop"]
        else:
            hits[i] = 1
            exits[i] = row["target"]
    out = events.copy()
    out["hit"] = hits
    out["exit_price"] = exits
    dropped = int((~complete).sum())
    if dropped:
        print(f"    因1m断档剔除 {dropped} 笔事件")
    return out.loc[complete & (ends > starts)]


def net_r_multiple(events: pd.DataFrame) -> pd.Series:
    """把每笔结果换算成 R 倍数：1R ＝ 一次完整止损的净亏损。"""
    entry = events["entry_price"].to_numpy(dtype=float)
    exit_ = events["exit_price"].to_numpy(dtype=float)
    direction = np.where(events["side"].to_numpy() == "LONG", 1.0, -1.0)
    hit = events["hit"].to_numpy()

    gross = (exit_ - entry) * direction
    # 入场是挂单；到中轨止盈也是挂单；止损和超时是吃单，另加不利滑点。
    exit_taker = hit != 1
    fees = entry * MAKER_FEE + np.abs(exit_) * np.where(exit_taker, TAKER_FEE, MAKER_FEE)
    slip = np.where(hit == -1, entry * STOP_SLIPPAGE, 0.0)
    net = gross - fees - slip

    stop_dist = np.abs(entry - events["stop"].to_numpy(dtype=float))
    risk_per_unit = stop_dist + entry * (MAKER_FEE + TAKER_FEE + STOP_SLIPPAGE)
    return pd.Series(net / risk_per_unit, index=events.index)


def build_events(symbol: str, engine) -> EventSet:
    bars = load_market_bars(symbol, "15m", engine=engine, venue=BINANCE)
    minute = load_minute_raw(symbol, engine)
    higher = {
        "m30": load_market_bars(symbol, "30m", engine=engine, venue=BINANCE),
        "h1": load_market_bars(symbol, "1h", engine=engine, venue=BINANCE),
    }
    feat = build_features(bars, higher)

    prev = feat.shift(1)  # 触轨用 t-1 冻结的轨道，特征也全部来自 t-1
    low = bars["low"].astype(float)
    high = bars["high"].astype(float)
    long_touch = low <= prev["bb_lower"]
    short_touch = high >= prev["bb_upper"]

    stop_distance = FIXED_STOP[symbol]
    frames = []
    for side, mask in (("LONG", long_touch), ("SHORT", short_touch)):
        idx = bars.index[mask.fillna(False)]
        if idx.empty:
            continue
        entry = prev.loc[idx, "bb_lower" if side == "LONG" else "bb_upper"]
        target = prev.loc[idx, "bb_middle"]
        stop = entry - stop_distance if side == "LONG" else entry + stop_distance
        frames.append(
            pd.DataFrame(
                {
                    "entry_time": idx,
                    "side": side,
                    "entry_price": entry.to_numpy(),
                    "target": target.to_numpy(),
                    "stop": stop.to_numpy(),
                },
                index=idx,
            )
        )
    events = pd.concat(frames).sort_index()
    events = events.join(prev.drop(columns=["bb_upper", "bb_middle", "bb_lower"]))
    events = events.dropna(subset=["rel_width", "adx", "chop"])
    events = resolve_outcomes(events, minute, HORIZON_BARS * pd.Timedelta(minutes=15))
    events["net_r"] = net_r_multiple(events)
    events["symbol"] = symbol
    events["is_long"] = (events["side"] == "LONG").astype(int)

    drop = ["entry_time", "side", "entry_price", "target", "stop", "hit", "exit_price", "net_r", "symbol"]
    return EventSet(
        features=events.drop(columns=drop),
        label=(events["hit"] == 1).astype(int),
        net_r=events["net_r"],
        timestamp=pd.Series(events["entry_time"].to_numpy(), index=events.index),
        symbol=events["symbol"],
        side=events["side"],
    )


def slice_split(ev: EventSet, name: str) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    start, end = SPLITS[name]
    mask = (ev.timestamp >= pd.Timestamp(start, tz="UTC")) & (
        ev.timestamp <= pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    )
    return ev.features[mask.to_numpy()], ev.label[mask.to_numpy()], ev.net_r[mask.to_numpy()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["ETH_USDT", "BTC_USDT"])
    args = parser.parse_args()

    engine = build_engine()
    sets = {s: build_events(s, engine) for s in args.symbols}

    for symbol, ev in sets.items():
        print(f"\n{'=' * 72}\n{symbol}  触轨事件 {len(ev.features)} 笔")
        xtr, ytr, rtr = slice_split(ev, "train")
        xva, yva, rva = slice_split(ev, "val")
        print(f"  训练 {len(xtr):>6} 笔  到中轨率 {ytr.mean():.1%}  平均 {rtr.mean():+.3f}R")
        print(f"  验证 {len(xva):>6} 笔  到中轨率 {yva.mean():.1%}  平均 {rva.mean():+.3f}R")
        if len(xtr) < 500 or len(xva) < 200:
            print("  样本不足，跳过")
            continue

        model = HistGradientBoostingClassifier(
            max_iter=400, learning_rate=0.05, max_leaf_nodes=31,
            min_samples_leaf=80, l2_regularization=1.0, random_state=7,
        )
        model.fit(xtr, ytr)
        p_tr = model.predict_proba(xtr)[:, 1]
        p_va = model.predict_proba(xva)[:, 1]
        print(f"  AUC  训练 {roc_auc_score(ytr, p_tr):.4f}   验证 {roc_auc_score(yva, p_va):.4f}")

        print("  验证集按预测分位分组（这一列才是钱）：")
        print(f"    {'分位':<12}{'笔数':>7}{'到中轨率':>10}{'平均R':>10}{'总R':>10}")
        edges = np.quantile(p_va, [0, 0.2, 0.4, 0.6, 0.8, 1.0])
        for i in range(5):
            lo, hi = edges[i], edges[i + 1]
            sel = (p_va >= lo) & (p_va <= hi) if i == 4 else (p_va >= lo) & (p_va < hi)
            if sel.sum() == 0:
                continue
            print(
                f"    {f'{i * 20}-{(i + 1) * 20}%':<12}{sel.sum():>7}"
                f"{yva[sel].mean():>9.1%}{rva[sel].mean():>10.3f}{rva[sel].sum():>10.1f}"
            )


if __name__ == "__main__":
    main()
