#!/bin/zsh

# 双击本文件可在macOS Terminal中启动Gate＋币安双行情影子服务；关闭Terminal即停止。
# 行情拉取已搬到采集服务，本进程只消费 Redis。每秒读一次收线水位，没变就整轮跳过
# （约0.5毫秒），有新K线才读全量并跑策略（约130毫秒）。因此1秒轮询的代价极小，
# 而收线到决策的延迟从最坏25秒降到1秒以内。
cd "/Users/stephen/Dev/Projects/gold-crypto-quant" || exit 1
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"
export PYTHONUTF8="1"
export PYTHONIOENCODING="utf-8"
# 输出经管道交给 tee，Python 默认会块缓冲，日志要攒够几KB才吐一次。
# 前台盯盘必须关掉缓冲，否则看到的不是实时。
export PYTHONUNBUFFERED="1"

exec .venv/bin/python main.py public-market-comparison \
  --limit 500 \
  --poll-seconds 1 \
  --max-cycles 10080 2>&1 | tee -a logs/public-market-comparison.log
