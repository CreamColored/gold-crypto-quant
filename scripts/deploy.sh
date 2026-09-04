#!/bin/zsh
# 从 Mac 发版到日本节点。
#
# 不做 push 自动部署：这套系统跑的是交易逻辑，push 之后立刻生效意味着少了一道
# 人为确认。今天一天改了十几次，每次都是"改完→跑测试→重启→验证"，这个节奏值得
# 保留成命令，而不是省掉。
#
# 用法：scripts/deploy.sh [--skip-tests]
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${GCQ_HOST:-gcq}"          # 在 ~/.ssh/config 里配好别名
REMOTE_DIR="${GCQ_DIR:-gold-crypto-quant}"
SERVICES=(gcq-collector gcq-quant gcq-web)

say() { print -P "%F{cyan}▸%f $*"; }
die() { print -P "%F{red}✗%f $*"; exit 1; }

# ---- 1. 本地门禁 ----
if [[ "${1:-}" != "--skip-tests" ]]; then
  say "跑测试"
  .venv/bin/python -m pytest tests -q -p no:warnings >/dev/null || die "测试没过，不发版"
  .venv/bin/python -m ruff check src tests --output-format concise \
    | grep -v bollinger_range | grep -qE "^src|^tests" && die "ruff 有新问题，不发版"
fi
[[ -n "$(git status --porcelain)" ]] && die "有未提交的改动，先提交"
say "推送到 GitHub"
git push origin main

# ---- 2. 发版前先看仓位 ----
say "检查远端持仓"
POS=$(ssh "$HOST" "cd $REMOTE_DIR && .venv/bin/python -c \"
import json,glob
out=[]
for p in glob.glob('.runtime/bollinger-*-live-paper-v5.json'):
    d=json.load(open(p))
    for s,q in d['positions'].items():
        if q.get('position_side'): out.append(f\\\"{p.split('-')[1]} {s} {q['position_side']}\\\")
print('；'.join(out) if out else '')
\"" 2>/dev/null || echo "")
if [[ -n "$POS" ]]; then
  print -P "%F{yellow}⚠%f 当前有持仓：$POS"
  print -n "  重启会中断约10秒（状态文件保留仓位，游标会补跑停机期间的K线）。继续？[y/N] "
  read -r ans; [[ "$ans" == "y" ]] || die "已取消"
fi

# ---- 3. 远端更新 ----
say "拉取代码"
BEFORE=$(ssh "$HOST" "cd $REMOTE_DIR && git rev-parse HEAD")
ssh "$HOST" "cd $REMOTE_DIR && git pull --ff-only origin main"
AFTER=$(ssh "$HOST" "cd $REMOTE_DIR && git rev-parse HEAD")
[[ "$BEFORE" == "$AFTER" ]] && say "代码没变化，跳过重启" && exit 0

# 依赖只在 pyproject 变了才装——装一次要几分钟，不该每次发版都等
if ssh "$HOST" "cd $REMOTE_DIR && git diff --name-only $BEFORE $AFTER" | grep -q pyproject.toml; then
  say "pyproject 有变化，重装依赖"
  ssh "$HOST" "cd $REMOTE_DIR && .venv/bin/pip install -q -e ."
fi

# ---- 4. 重启：采集在前，量化在后 ----
say "重启服务"
# 服务以 root 运行，systemd 是系统级单元，不需要 sudo。
ssh "$HOST" "systemctl restart ${SERVICES[*]}"

# ---- 5. 验证：不能只看 systemctl active ----
# 崩溃重启循环里 systemctl 也会短暂显示 active。必须确认它真的产出了新日志。
say "等待服务产出日志"
sleep 20
for s in "${SERVICES[@]}"; do
  state=$(ssh "$HOST" "systemctl is-active $s" || true)
  [[ "$state" == "active" ]] || die "$s 状态是 $state"
  lines=$(ssh "$HOST" "journalctl -u $s --since '30 seconds ago' --no-pager -q | wc -l")
  [[ "$lines" -gt 0 ]] || die "$s 进程在，但30秒内没有任何日志——可能卡住了"
  print -P "  %F{green}✓%f $s  最近30秒 $lines 行日志"
done
say "发版完成：${AFTER:0:7}"
