#!/usr/bin/env bash
# Mac 本地测试环境：与日本服务器上的生产实例完全隔离地跑同一套服务。
#
# 隔离靠三层，缺一不可：
#   1. MySQL  —— 用 Mac 本地 127.0.0.1:3306/quant。迁移之后这份库已无人读写，
#                生产在服务器上有自己的库，两边不会互相污染。
#   2. Redis  —— 换 db 索引和 key 前缀，即使将来开了 remote.sh 的隧道也撞不上。
#   3. 通知   —— 清空钉钉与邮件配置，测试实例永远不对外发消息。
#   4. 状态   —— 影子账户落在磁盘 JSON 上，指到 .runtime/testenv/，
#                不覆盖 .runtime/ 下那份迁移到服务器时的快照。
#
# 不改 .env：.env 是被 git 跟踪的生产配置，改它会在合回 main 时把测试参数带上服务器。
# 真实环境变量的优先级高于 .env（pydantic-settings 的默认行为），所以这里只做覆盖。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/.runtime/testenv"
STATE_DIR="$ROOT/.runtime/testenv/state"
PY="$ROOT/.venv/bin/python"

# ---- 隔离层 ----
export APP_ENV="test"
export REDIS_DB="1"                 # 生产用 0
export REDIS_KEY_PREFIX="gcqtest"   # 生产用 gcq（代码自动补冒号）
export WEB_HOST="127.0.0.1"         # 只监听本机，测试控制台不暴露到局域网
export WEB_PORT="8766"              # 生产用 8765
export DINGTALK_WEBHOOK=""          # 测试实例禁止对外发消息
export DINGTALK_SECRET=""
export STATUS_EMAIL_TO=""
export SMTP_HOST=""
export LIVE_TRADING="false"         # 影子模拟，永不下真单
export GCQ_STATE_DIR="$STATE_DIR"   # 生产用 .runtime/

# ---- 启动参数 ----
# quant 跑的是震荡 v1.0 / 顺势 v1.0 的双策略对照，不再是 Gate/币安双交易所对照。
# 策略按已收线K线决策，最小驱动周期 15m，所以 10 秒查一次游标就够，
# 服务器上那套 1 秒轮询是给在途触轨用的，这里不需要。
declare -a NAMES=(collector quant web)
cmdline() {
  case "$1" in
    collector) echo "quote-collector" ;;
    quant)     echo "strategy-comparison --poll-seconds 10 --max-cycles 604800" ;;
    web)       echo "web-run" ;;
    *) echo "未知服务：$1" >&2; return 1 ;;
  esac
}

# GNU 与 BSD 的 stat 参数不兼容：macOS 是 -f %m，Linux/WSL2 是 -c %Y。
# 探测一次而不是按 uname 分支——WSL2 里 uname 报 Linux，但同一台机器上
# 可能同时有 GNU coreutils 和 busybox，直接试哪个能用最可靠。
if stat -c %Y . >/dev/null 2>&1; then
  mtime() { stat -c %Y "$1"; }
else
  mtime() { stat -f %m "$1"; }
fi

pidfile() { echo "$LOG_DIR/$1.pid"; }
logfile() { echo "$LOG_DIR/$1.log"; }

alive() {
  local pf; pf="$(pidfile "$1")"
  [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null
}

start_one() {
  local name="$1"
  if alive "$name"; then
    echo "  $name 已在运行（pid $(cat "$(pidfile "$name")")）"
    return
  fi
  mkdir -p "$LOG_DIR" "$STATE_DIR"
  # shellcheck disable=SC2046
  nohup "$PY" "$ROOT/main.py" $(cmdline "$name") >>"$(logfile "$name")" 2>&1 &
  echo $! >"$(pidfile "$name")"
  echo "  $name 已启动（pid $!）"
}

stop_one() {
  local name="$1" pf; pf="$(pidfile "$name")"
  if ! alive "$name"; then
    echo "  $name 未在运行"
    rm -f "$pf"
    return
  fi
  local pid; pid="$(cat "$pf")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do alive "$name" || break; sleep 0.5; done
  alive "$name" && kill -9 "$pid" 2>/dev/null || true
  rm -f "$pf"
  echo "  $name 已停止"
}

# 只看进程在不在是不够的：崩溃重启循环里进程也一直在。
# 和 deploy.sh 同一个原则——必须确认它真的还在产出新日志。
status_one() {
  local name="$1" lf; lf="$(logfile "$name")"
  if ! alive "$name"; then
    printf "  %-10s 停止\n" "$name"
    return
  fi
  local pid age
  pid="$(cat "$(pidfile "$name")")"
  if [[ -f "$lf" ]]; then
    age=$(( $(date +%s) - $(mtime "$lf") ))
    printf "  %-10s 运行中  pid %-7s 日志 %ss 前更新\n" "$name" "$pid" "$age"
  else
    printf "  %-10s 运行中  pid %-7s 尚无日志\n" "$name" "$pid"
  fi
}

case "${1:-status}" in
  start)
    echo "启动 Mac 测试环境（Redis db=$REDIS_DB 前缀=$REDIS_KEY_PREFIX Web=$WEB_PORT）"
    for n in "${NAMES[@]}"; do start_one "$n"; done
    ;;
  stop)
    for n in "${NAMES[@]}"; do stop_one "$n"; done
    ;;
  restart)
    for n in "${NAMES[@]}"; do stop_one "$n"; done
    for n in "${NAMES[@]}"; do start_one "$n"; done
    ;;
  status)
    echo "Mac 测试环境："
    for n in "${NAMES[@]}"; do status_one "$n"; done
    ;;
  log)   tail -n "${3:-200}" "$(logfile "${2:-quant}")" ;;
  tail)  tail -f "$(logfile "${2:-quant}")" ;;
  env)   env | grep -E "^(APP_ENV|REDIS_DB|REDIS_KEY_PREFIX|WEB_HOST|WEB_PORT|LIVE_TRADING|GCQ_STATE_DIR)=" | sort ;;
  *)
    echo "用法: $0 {start|stop|restart|status|log [服务] [行数]|tail [服务]|env}" >&2
    echo "服务: ${NAMES[*]}" >&2
    exit 1
    ;;
esac
