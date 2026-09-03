#!/bin/zsh

# 双击本文件可在macOS Terminal中启动盘口采集；关闭Terminal即停止（走SIGTERM优雅收尾）。
# 只采集Gate与币安的bookTicker并按秒/按分钟聚合入库，不参与任何交易判定。
cd "/Users/stephen/Dev/Projects/gold-crypto-quant" || exit 1
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"
export PYTHONUTF8="1"
export PYTHONIOENCODING="utf-8"
# 输出经管道交给 tee，不关缓冲看到的就不是实时日志。
export PYTHONUNBUFFERED="1"

exec .venv/bin/python main.py quote-collector 2>&1 | tee -a logs/quote-collector.log
