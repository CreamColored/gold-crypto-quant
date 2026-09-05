# 策略 V5.8 完整备份

冻结于 2026-09-05。这是新策略（震荡 v1.0 / 顺势 v1.0）分叉前的生产版本，
日本服务器当时正在运行它。

## 怎么恢复

```bash
git checkout strategy-v5.8        # 标签指向 7b6b547
```

本目录另存了三样东西，用于在没有 git 的情况下复原：

| 文件 | 内容 |
|---|---|
| `src/multi_timeframe_rotation_simulator.py` | 策略主体，79KB |
| `src/bollinger_signal_cycle.py` | 单轮驱动 |
| `src/public_market_comparison_runner.py` | Gate/币安双影子对照服务 |
| `state-gate-9836.60U.json` | 迁移到服务器时的 Gate 影子账户快照 |
| `state-binance-9852.39U.json` | 同上，币安 |
| 本文件 | 规则的文字规格 |

---

## 一句话

布林带震荡箱体轮转：判定出"箱体"后，下轨买、上轨卖，赌均值回归；
全系统同时最多持有一笔仓位。

## 版本标识

`MULTI_ROTATION_STRATEGY_VERSION = "5.8.0"`

## 周期与品种

```
INTERVAL_PRIORITY       = ("5m", "15m", "30m", "1h")   # 参与顶底结构判断
ENTRY_INTERVAL_PRIORITY = ("15m", "30m", "1h")         # 只有这三个能触发交易
SYMBOL_PRIORITY         = ("BTC_USDT", "ETH_USDT")
```

5m 在 V5.8 被移出入场周期，但仍参与结构判断。
遍历是**平铺优先级**：按顺序取第一个合格的周期开仓，不检查其他周期方向。

## 箱体资格

每根已收盘 K 线重新判定，不允许沿用失效的旧箱体：

```
箱体成立 = box_candidate 且 未突破 且 该周期未处于止损封锁
```

其中"布林带开口"（`_bands_are_opening`）指上轨升、下轨降、带宽同时扩大，
开口即视为不再是震荡。

## 入场

- 价格触及下轨 → 做多；触及上轨 → 做空
- 触轨用**在途 K 线**（第 501 根，由 bookTicker 中价合成）实时判定
- **停留确认**：条件必须连续成立 `provisional_dwell_seconds = 3.0` 秒。
  判据是当前 `close` 仍在轨外，不是累计的 high/low
  （用累计极值会导致一旦触碰就永远为真，等于没有过滤）
- 同一根 1 分钟 K 线内不重复开仓（`last_micro_bar_times` 游标）

## 止损

固定点数，不分周期：

```
FIXED_STOP_DISTANCE = { "BTC_USDT": 300.0 点, "ETH_USDT": 12.0 点 }
其他品种：OTHER_SYMBOL_STOP_RETURN = 1.00（浮亏 100%），
          按 PAPER_LEVERAGE = 125 换算 = 价格反向 0.8%
```

止损阶梯依次推进：`开仓价 → 开仓时中轨 → 开仓时对侧轨 → 上一次减仓价`。

开仓当根 1 分钟 K 线跳过止损判定——该 K 线收线后带回的极值可能发生在开仓之前。

## 减仓与止盈

```
MIDDLE_REDUCE_RATIO = 0.30    # 中轨第一次减仓，有无结构都是 30%
LADDER_REDUCE_RATIO = 0.50    # 第二次及以后
STRUCTURE_LADDER_STEP_POINTS = 8.0   # ETH 8 点，BTC 按价格比例放大
```

到达对侧轨时分两种情况：

- `structure_confirmed = False` → **全部平仓止盈**，箱体仍有效则触轨即时反手
- `structure_confirmed = True`（开仓时命中反方向顶/底结构）→ 不止盈反手，
  改为减仓 50%、止损收紧到中轨，进入阶梯延续：每 8 点减仓一次

**止盈目标取的是当前 K 线的 `bb_upper`/`bb_lower`，逐根移动**——
这一点在十三节课程复盘中被列为"49 小时 0 次对侧轨止盈"的疑似成因。

## 顶底结构

按 MACD 红绿柱**分组**比较（不是逐根）：

```
STRUCTURE_MACD_FAST/SLOW/SIGNAL = 12 / 26 / 9
STRUCTURE_MACD_HISTORY = 150

顶部结构 = 本组红柱区收盘价更高、但 DIF 更低，在红柱区结束那根 K 线确认
底部结构 = 本组绿柱区收盘价更低、但 DIF 更高，在绿柱区结束那根 K 线确认
```

判定是**离散的**——只在柱区刚结束时成立一次，不是每根 K 线重复判定。

与课程定义的三处差异（见 `docs/课程规则提取.md`）：
无趋势前提、无"至少两根"浪形门槛、三个阶段压成了一个。

## 仓位与风控

```
risk_per_trade   = 0.0025          # 单笔风险 0.25%
仓位数量          = equity × risk_per_trade ÷ 止损距离
initial_equity   = 10_000.0
MAKER_FEE_RATE   = 0.0002          # 0.02%
TAKER_FEE_RATE   = 0.0005          # 0.05%
stop_slippage_rate = 0.0002
```

手续费**不要**改成负的 maker 返佣——那是 VIP4 以上才有的。
2026-09-03 实测：73 笔成交、名义额 32.2 万 U，返佣假设让手续费少算 47.95U，
足以把当天的 +18.73U 翻成 -29.22U。

放宽止损等于缩小仓位，每单风险恒定为账户的 0.25%。

## 熔断

- 日亏熔断：`daily_blocked`
- 永久保险丝：`permanent_fuse`
- 止损封锁：打止损后 `symbol_blocked_after_stop`，**按品种封锁而非按周期**，
  解除需要重新出现合格箱体。这是 2026-09-04 非农两笔止损后
  BTC/ETH 长期不开单的直接原因。
- 宏观静默：NFP / CPI / FOMC 前后各 15 分钟不开仓（`config/macro-events.json`）

## 已知的实测结论

- **49 小时 0 次对侧轨止盈**，设计中的"下轨买→中轨减→上轨卖"几乎从不走完
- 反手过滤器（刚出柱区或 ≤4 根不反手）只挡住 8 次反手中的 1 次，盈亏零变化
- 秒级回测与 1m 回测在固定窗口下逐字节相同——模拟器按轨价成交，
  与触轨在一分钟内何时被发现无关
