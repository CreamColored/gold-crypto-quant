# 迁移到 Windows + WSL2

目标：Windows 接手**开发机与测试环境**（跑震荡 v1.0 / 顺势 v1.0 的双策略对照），
日本服务器继续跑 V5.8 产出对照数据，不动。

选 WSL2 而不是原生 Windows 的原因：`fcntl`（单实例锁）、三个 bash 脚本、
systemd 风格的运维在 WSL2 里原样可用，几乎零移植成本。代价是多一层虚拟化，
IDE 要用 Remote-WSL 模式。

---

## 一、代码侧已经改好的（提交 `<本次提交>`）

迁移前代码里有三处只在 macOS 成立的写法，已经修掉：

| 位置 | 问题 | 处理 |
|---|---|---|
| `scripts/testenv.sh:96` | `stat -f %m` 是 BSD 专有，Linux/WSL2 上直接报错 | 启动时探测一次，GNU 用 `-c %Y`、BSD 用 `-f %m` |
| `web/data.py` | `os.uname()` 只有 POSIX 有 | 换成 `platform.node()` |
| `notifications/smtp_mail.py` | 系统标签写死 "macOS" | 换成 `platform.platform(terse=True)` |

`fcntl` 没动——WSL2 是 Linux，它就在。**如果将来要跑原生 Windows**，
`runtime/market_data_runner.py` 的 `SingleInstanceLock` 需要换成 `msvcrt.locking`，
而且因为 `runtime/__init__.py` 在包级别 import 了它，三个服务全部会受影响。

---

## 二、前置

1. **WSL2 + Ubuntu**（PowerShell 管理员）
   ```
   wsl --install -d Ubuntu
   ```
   装完重启，进 Ubuntu 设好用户名密码。

2. **Docker Desktop**，设置里开 *Use the WSL 2 based engine*，
   并在 *Resources → WSL Integration* 里勾上 Ubuntu。

3. **确认版本**（在 WSL2 的 Ubuntu 里）
   ```
   wsl.exe --version     # 需要 WSL 2
   docker --version
   ```

---

## 三、容器：一处必须改

把 Mac 上的 `~/Dev/Tools/Docker/{MySQL,Redis}/compose.yaml` 复制过来，
**Redis 那份必须改一行**：

```yaml
ports:
  - "127.0.0.1:6379:6379"
  - "192.168.0.122:6379:6379"   # ← 这是 Mac 的局域网 IP
```

`192.168.0.122` 在 Windows 上不存在，容器会起不来。二选一：

- 局域网里没有别的机器要连 Redis → **删掉第二行**
- 还要留局域网访问 → 换成 Windows 这台的局域网 IP

绑具体地址而不是 `0.0.0.0` 是有意的：换到陌生网络时容器起不来，
而不是把 6379 暴露出去。这个设计要保留。

MySQL 那份不用改（只绑 3306，没有写死 IP）。

起容器：
```
cd ~/Dev/Tools/Docker/MySQL && docker compose up -d
cd ~/Dev/Tools/Docker/Redis && docker compose up -d
docker ps
```

---

## 四、数据库：建库建用户

容器起来后需要建 `quant` 库和 `quant_app` 用户。root 密码在 MySQL 的
`compose.yaml` 里，`.env` 里的 `DATABASE_URL` 用的是 `quant_app` 的密码。

```
docker exec -it mysql mysql -uroot -p
```

进去后执行（把 `<quant_app的密码>` 换成 `.env` 里 `DATABASE_URL` 中的那个）：

```sql
CREATE DATABASE IF NOT EXISTS quant
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'quant_app'@'%' IDENTIFIED BY '<quant_app的密码>';
GRANT ALL PRIVILEGES ON quant.* TO 'quant_app'@'%';
FLUSH PRIVILEGES;
```

表结构不用手工建，服务启动时 `create_all` 会自己长出来。

---

## 五、项目

```
git clone <仓库地址> ~/gold-crypto-quant
cd ~/gold-crypto-quant
git checkout course-rules
```

`.env` 跟着仓库一起过来（本项目有意把它纳入版本管理），
里面的 `DATABASE_URL`、`REDIS_*` 都指向 `127.0.0.1`，在新机器上直接成立，不用改。

Python 3.12（系统 Python 可能是 3.13，本项目要求 `>=3.12,<3.13`）：
```
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.12
VIRTUAL_ENV=.venv uv pip install -e .
```

验证：
```
.venv/bin/python -m pytest -q -p no:warnings
```

---

## 六、行情数据：重跑，不要搬库

Mac 上有 29 万行 K 线，但**不要 dump/restore**——直接重拉更快更干净：

```
.venv/bin/python scripts/backfill_gate_history.py --days 180
.venv/bin/python scripts/backfill_gate_history.py --days 180 --intervals 1m,5m
```

两条合计约 **80 秒**，拿到的深度和 Mac 上完全一样
（Gate 每周期上限 10000 根：1m 6.9 天、5m 34.7 天、15m 104 天、30m/1h 180 天）。
脚本是幂等的，中断了直接重跑。

影子账户状态不用迁——两个策略当前都是 10000U、零成交，
新机器上第一次启动会自己建。真要保留就把 `.runtime/testenv/state/*.json` 拷过去。

---

## 七、启动与验证

```
./scripts/testenv.sh start
./scripts/testenv.sh status
```

三项都要对：

1. **服务在跑且日志在产出**
   `status` 不只看进程在不在——崩溃重启循环里进程也一直在，
   要看"日志 N 秒前更新"那一列。

2. **隔离生效**
   ```
   ./scripts/testenv.sh env
   ```
   应该看到 `REDIS_DB=1`、`REDIS_KEY_PREFIX=gcqtest`、`WEB_PORT=8766`、
   `LIVE_TRADING=false`、`GCQ_STATE_DIR=.../testenv/state`。

3. **Web 能开**
   浏览器访问 `http://localhost:8766`。WSL2 的 localhost 会自动转发到 Windows，
   不需要额外配置。对照页在 `/compare`。

---

## 八、和 Mac 的差异

| | Mac | Windows + WSL2 |
|---|---|---|
| Docker | OrbStack | Docker Desktop（WSL2 后端） |
| `stat` | BSD `-f %m` | GNU `-c %Y`（脚本自动探测） |
| Redis 局域网绑定 | `192.168.0.122` | 必须改，否则容器起不来 |
| 到 Gate 的延迟 | 约 1390ms | 视网络而定，回测不受影响 |
| 文件性能 | 原生 | **代码必须放在 WSL2 文件系统里**（`~/`），放在 `/mnt/c/` 下会慢一个数量级 |

最后一条最容易踩：把仓库 clone 到 `/mnt/c/Users/...` 下面，
pytest 和回测会慢到不能用。一定要放在 WSL2 自己的 `~/` 里。

---

## 九、迁移后 Mac 怎么办

测试环境搬走之后 Mac 上可以：
```
./scripts/testenv.sh stop
cd ~/Dev/Tools/Docker/MySQL && docker compose stop
cd ~/Dev/Tools/Docker/Redis && docker compose stop
```
建议留一周再删数据卷，确认 Windows 那边稳定。日本服务器全程不受影响。
