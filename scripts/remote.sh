#!/bin/zsh
# 日本节点的远程操作入口。
#
# 不把日志传回 Mac：那是在搭基础设施解决一个不存在的问题。ssh 过去直接读就是实时的，
# 零组件、零延迟、零维护。
#
# 更要紧的是 tunnel 那条：开了隧道之后，本机所有诊断脚本（查MySQL、查Redis、
# 算指标）不用改一行就能对着服务器的数据跑。这比"把日志弄过来"有用得多——
# 今天排查那两次故障，靠的都是现场查库查缓存，不是翻日志。
#
# 用法：
#   scripts/remote.sh log quant [行数]     看日志
#   scripts/remote.sh tail quant           实时跟随
#   scripts/remote.sh err                  只看三个服务的报错
#   scripts/remote.sh status               服务、容器、资源一览
#   scripts/remote.sh tunnel               开数据库与Redis隧道
#   scripts/remote.sh tunnel-stop          关隧道
#   scripts/remote.sh sh "任意命令"
set -euo pipefail
HOST="${GCQ_HOST:-gcq}"
DIR="${GCQ_DIR:-gold-crypto-quant}"
# 本地端口刻意不用 3306/6379：Mac 上本来就跑着同名服务，撞端口会让脚本连错库。
DB_PORT=13306
REDIS_PORT=16379

unit() {
  case "$1" in
    quant|q)      echo gcq-quant ;;
    collector|c)  echo gcq-collector ;;
    web|w)        echo gcq-web ;;
    *)            echo "$1" ;;
  esac
}

case "${1:-status}" in
  log)   ssh "$HOST" "journalctl -u $(unit ${2:-quant}) -n ${3:-200} --no-pager -o short-iso" ;;
  tail)  ssh -t "$HOST" "journalctl -u $(unit ${2:-quant}) -f -o short-iso" ;;
  err)
    ssh "$HOST" "journalctl -u gcq-quant -u gcq-collector -u gcq-web \
      --since '${2:-2 hours ago}' --no-pager -o short-iso \
      | grep -iE '失败|异常|错误|error|timeout|traceback|断开' | tail -60" ;;
  status)
    ssh "$HOST" "
      echo '=== 服务 ==='
      for s in gcq-collector gcq-quant gcq-web cloudflared; do
        printf '  %-16s %s  已运行 %s\n' \"\$s\" \"\$(systemctl is-active \$s 2>/dev/null)\" \
          \"\$(systemctl show \$s -p ActiveEnterTimestamp --value 2>/dev/null | cut -d' ' -f2-3)\"
      done
      echo; echo '=== 容器 ==='
      docker stats --no-stream --format '  {{.Name}}  CPU {{.CPUPerc}}  内存 {{.MemUsage}}' 2>/dev/null
      echo; echo '=== 系统 ==='
      printf '  负载 %s\n' \"\$(uptime | sed 's/.*load average[s]*: //')\"
      free -m | awk 'NR==2{printf \"  内存 %d/%d MB（%.0f%%）\n\", \$3, \$2, \$3/\$2*100}'
      df -h / | awk 'NR==2{printf \"  磁盘 %s/%s（%s）\n\", \$3, \$2, \$5}'
      echo; echo '=== 最近一轮 ==='
      journalctl -u gcq-quant -n 2 --no-pager -q -o cat
    " ;;
  tunnel)
    pgrep -f "ssh.*$DB_PORT:127.0.0.1:3306" >/dev/null && { echo "  隧道已在运行"; exit 0; }
    ssh -f -N -o ExitOnForwardFailure=yes \
      -L "$DB_PORT:127.0.0.1:3306" -L "$REDIS_PORT:127.0.0.1:6379" "$HOST"
    cat > .env.remote <<ENV
# 隧道生效期间用这份配置，让本机脚本读服务器的数据。
# 用法：set -a; source .env.remote; set +a; 然后照常跑任何分析脚本。
DATABASE_URL=mysql+pymysql://\${DB_USER}:\${DB_PASSWORD}@127.0.0.1:$DB_PORT/quant?charset=utf8mb4
REDIS_HOST=127.0.0.1
REDIS_PORT=$REDIS_PORT
ENV
    echo "  MySQL  → 127.0.0.1:$DB_PORT"
    echo "  Redis  → 127.0.0.1:$REDIS_PORT"
    echo "  已写出 .env.remote（账号密码需自行填入 DB_USER / DB_PASSWORD）" ;;
  tunnel-stop)
    pkill -f "ssh.*$DB_PORT:127.0.0.1:3306" && echo "  隧道已关闭" || echo "  没有运行中的隧道" ;;
  sh)    shift; ssh "$HOST" "cd $DIR && $*" ;;
  *)     echo "用法：log|tail|err|status|tunnel|tunnel-stop|sh"; exit 1 ;;
esac
