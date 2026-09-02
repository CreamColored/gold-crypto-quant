#!/bin/zsh

# 双击本文件可在macOS Terminal中启动只读Web监管后台；关闭Terminal即停止服务。
cd "/Users/stephen/Dev/Projects/gold-crypto-quant" || exit 1
export LANG="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"
export PYTHONUTF8="1"
export PYTHONIOENCODING="utf-8"

# 调用统一CLI入口，页面只读取MySQL和影子状态，不具备交易所下单方法。
exec .venv/bin/python main.py web-run 2>&1 | tee -a logs/web-ui.log
