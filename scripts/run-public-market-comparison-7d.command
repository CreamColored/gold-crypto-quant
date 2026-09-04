#!/bin/zsh

# 双击本文件可在macOS Terminal中启动Gate＋币安量化服务影子服务；关闭Terminal即停止。
# 行情拉取已搬到采集服务，本进程只消费 Redis。每秒跑一轮：在途K线每秒变一次，
# 触轨的停留确认要靠逐秒复查才能计时。健康表与日志只在有新收线K线时才动，
# 否则每秒30次数据库写入、每秒一行日志会把有用信息淹掉。
# max-cycles 按每秒一轮计，604800 约合七天。
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
  --max-cycles 604800 2>&1 | tee -a logs/public-market-comparison.log
