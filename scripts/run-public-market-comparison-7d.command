#!/bin/zsh

# 双击本文件可在macOS Terminal中启动Gate＋币安双行情影子服务；关闭Terminal即停止。
# --poll-seconds 决定新收盘的1分钟K线最迟多久被扫到；库在本地后单轮工作约9秒，
# 取20秒可保证每根1分钟K线收线后20秒内一定被处理，不会跨过整根。
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
  --poll-seconds 20 \
  --max-cycles 10080 2>&1 | tee -a logs/public-market-comparison.log
