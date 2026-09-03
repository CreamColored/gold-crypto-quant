# Gold Crypto Quant（Moon监管中心）

BTC/USDT、ETH/USDT与XAU/USDT（黄金）的量化研究和影子模拟交易系统。当前阶段用Gate
与币安各自的实盘公共行情（无需API密钥）并行运行同一套策略，对比两家交易所的信号、
交易、盈亏和账户权益，但不提交任何真实订单。OANDA黄金代码与历史数据保留，当前暂停
（`OANDA_ENABLED=false`）——现在的黄金敞口来自两家交易所的USDT结算黄金永续合约，
不是OANDA。`LIVE_TRADING` 必须永久保持为 `false`。

技术栈：Python 3.12、Pandas、vectorbt、MySQL 8、SQLAlchemy、PyMySQL、
FastAPI、Jinja2、ECharts。

## 当前运行规则

- 当前策略：布林带震荡箱体轨道轮转 `MULTI_ROTATION_STRATEGY_VERSION = 5.7.0`
  （权威实现见 `src/gold_crypto_quant/runtime/multi_timeframe_rotation_simulator.py`
  与 `src/gold_crypto_quant/strategy/bollinger_range.py`）
- 策略品种：BTC/USDT、ETH/USDT、XAU/USDT，可同时持仓；每个品种在每个交易所各自最多一笔仓位
- 策略周期：每个品种独立按 `5m → 15m → 30m → 1h` 固定优先级选取当前有效箱体
- 方向：多空双向
- 行情来源：Gate、币安各自的实盘公共行情，均无需API密钥，不提交真实订单
- 黄金：`XAU_USDT`（Gate）与 `XAUUSDT`（币安）USDT结算永续合约，走与BTC/ETH完全相同的
  公共行情客户端；币安该品种合约类型为 `TRADIFI_PERPETUAL`
- OANDA黄金：暂缓，`OANDA_ENABLED=false`
- 杠杆上限：125 倍；禁止补仓、摊平、多空双开
- 单笔账户风险：当前影子权益的0.25%
- 每日亏损熔断：北京时间当日起始权益的2%
- 最大回撤熔断：历史峰值的8%（永久熔断，需人工审核）
- Gate与币安账户完全隔离，各自初始影子权益10,000 USDT，先观察7天

完整策略细则、系统架构和运行检查步骤见 `claude-handoff/`（本地交接文档，
已在 `.gitignore` 中排除，不提交到仓库）。

## 本地启动

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e '.[dev]'
cp .env.example .env
python main.py
python main.py db-check
python main.py init-db
python main.py sync-comments

# 当前日常运行路径（V5.7，Gate+币安双所影子交易与Web监管）
python main.py public-market-comparison --poll-seconds 20   # 双行情影子服务，默认7天
python main.py web-init-admin                    # 首次创建Moon超级管理员
python main.py web-run                           # 启动Moon只读Web，默认127.0.0.1:8765
python main.py system-readiness
pytest
ruff check .

# 以下为单交易所（仅Gate）研究与历史命令，非当前日常运行路径
python main.py import-gate-bars --limit 1000
python main.py import-gate-bars --history-days 90 --limit 2000
python main.py backtest-bollinger --contracts ETH_USDT
python main.py qualify-bollinger --contracts ETH_USDT
# 以下EMA命令仅保留旧研究复现能力，不再具有运行准入权限
python main.py backtest-ema
python main.py diagnose-ema
python main.py research-ema --intervals 15m 30m 1h
python main.py walkforward-ema --intervals 15m 30m 1h
python main.py walkforward-filters --intervals 15m 30m
python main.py walkforward-pullback --intervals 15m 30m
python main.py walkforward-regime --intervals 5m 15m 30m 1h
python main.py walkforward-ema12 --intervals 15m 30m
python main.py walkforward-breakout --intervals 5m 15m 30m 1h
python main.py walkforward-joint-breakout --intervals 5m 15m 30m 1h
python main.py walkforward-joint-exits --intervals 30m
python main.py walkforward-joint-regime --intervals 30m
python main.py walkforward-joint-macro --intervals 30m
python main.py qualify-ema --intervals 15m 30m
python main.py qualify-breakout-fixed --intervals 30m
python main.py execution-safety
python main.py system-readiness
python main.py risk-snapshot
python main.py market-health
python main.py market-runner --limit 500
python main.py market-runner --limit 500 --enable-signals
python main.py market-runner-status
python main.py paper-signal-cycle
python main.py paper-simulation-status
# 仅执行一轮，用于部署前连接验证
python main.py market-runner --limit 500 --max-cycles 1
pytest
```

`--poll-seconds` 决定新收盘的1分钟K线最迟多久被扫到。库在本地后单轮工作只要约9秒，
所以取20秒——保证每根1分钟K线收线后20秒内一定被处理，不会跨过整根。日常启动脚本在
`.runtime/run-public-market-comparison-7d.command`，**该目录在 .gitignore 里、不受版本控制**，
改参数要直接改那个文件。

数据库连接使用 `mysql+pymysql`，字符集统一为 `utf8mb4`，应用和数据库时间统一使用
UTC。当前仓库按用户明确决定跟踪 `.env`；该文件含明文密钥，不得公开分享仓库或日志。

核心数据表包括：交易品种、K线、策略运行、信号、订单、成交、持仓、账户快照和
风控事件。信号使用 `dedupe_key`、订单使用 `client_order_id` 防止重复写入和重复下单。
每张表和每个字段都在 SQLAlchemy 模型中维护中文说明；已有数据库可通过
`python main.py sync-comments` 安全同步注释。

Gate历史K线导入默认覆盖 `BTC_USDT`、`ETH_USDT` 的5m、15m、30m和1h周期。
导入会跳过尚未收盘的K线，并根据数据库唯一键执行幂等新增或更新，因此可以安全重跑。
传入 `--history-days` 后，程序会以数据库中最早K线作为断点，按每页最多2000个时间点
继续向前回溯；每页写入后立即提交，网络中断时再次运行同一命令即可继续。
Gate测试网目前只允许访问每个周期最近10000个K线点；程序会自动把过早的请求截断并在
结果中标记。因此5分钟周期最多约34.7天，15分钟约104天，30分钟约208天，1小时约
416天。若研究需要更长历史，应后续接入独立历史数据源，不能改用实盘下单接口代替。

OANDA黄金模块当前由 `OANDA_ENABLED=false` 暂停，不参与系统就绪、策略准入和30天监督。
历史数据与代码均不删除，未来明确合法实盘渠道后再恢复。历史导入固定连接官方
`api-fxpractice.oanda.com` Practice 主机，使用
`XAU_USD` 中间价蜡烛并将 M5、M15、M30、H1 标准化为系统的四个周期。单页最多读取
5000根，未完成蜡烛不会入库；OANDA返回的 `volume` 是该周期内生成的价格数量，不是
集中交易所意义上的实际黄金成交量。OANDA客户端只提供行情、账户、品种规则和报价读取
方法，没有下单方法。`oanda-account-check` 会额外验证当前Practice账户是否实际开放
`XAU_USD`；能读取公共黄金K线并不等于该账户有黄金品种权限。

策略只使用已经收盘的同周期K线计算下一根可用轨道，避免未来数据。
125 倍仅为保证金杠杆上限，仓位由账户风险和止损距离决定。

## 当前布林带策略（V5.7）

当前唯一活动策略是 `MULTI_ROTATION_STRATEGY_VERSION = 5.7.0`。每个交易所独立对BTC和
ETH按 `5m → 15m → 30m → 1h` 寻找三轨走平的布林带箱体；下轨做多、上轨做空，中轨附近
减半并把剩余止损移到开仓价，对侧轨止盈，箱体仍有效时立即反手；止损后等待对应周期
完整收线并重新确认震荡才解除封锁。BTC按最近20根收盘价中位数相对2,500的比例等比例
放大走平阈值和最小带宽，ETH使用绝对点数。固定止损：BTC 5m/15m 250点、30m/1h 500点；
ETH 5m/15m 5点、30m/1h 10点。BTC与ETH可以同时持仓，但同一币种同一交易所内只能有
一笔仓位，禁止补仓、摊平和多空双开。

权威实现见 `src/gold_crypto_quant/runtime/multi_timeframe_rotation_simulator.py` 和
`src/gold_crypto_quant/strategy/bollinger_range.py`；完整规则、账户熔断和仓位计算细节
见本地交接文档 `claude-handoff/01-当前策略V5.7.md`。`docs/` 目录下的历史策略文档
（单ETH、`BOLLINGER_RANGE 4.0.0`、V4等）仅作历史审计保留，不代表当前运行策略。
`EMA_TREND` 相关批准记录同样只作为历史审计保留。

### 顶底结构确认后的阶梯延续

顶底结构按MACD红绿柱**分组**比较，不是按摆动点比较：

- **顶部结构**＝本组红柱区（柱值>0）的最高收盘价比上一组红柱区更高，但该组DIF最大值更低，
  在红柱区结束、走出死叉那根K线上确认
- **底部结构**＝本组绿柱区（柱值<0）的最低收盘价比上一组绿柱区更低，但该组DIF最小值更高，
  在绿柱区结束、走出金叉那根K线上确认
- 结构是**持续状态**而非瞬时信号：顶部结构一直有效到下一次金叉，底部结构一直有效到下一次死叉
- 判定用标准MACD(12,26,9)

检测范围是**全部七个周期：1m / 3m / 5m / 10m / 15m / 30m / 1h**，与该仓位在哪个周期交易无关——
做5分钟震荡要看1小时，做1小时也要看1分钟。任一周期命中即成立；顶底同时出现时不给方向，
避免多空两边都被判成顺结构。1m、3m、10m 由1分钟K线按UTC自然边界重采样得到。

结构与仓位方向**同向**时（多单遇底部结构、空单遇顶部结构）转入阶梯延续。开仓那一刻没有结构
也会每分钟复查一次——结构可能在持仓中、甚至在反手之后才形成，一旦确认就保持。

命中后的仓位管理（以空单为例，多单对称）：

1. 中轨：减仓50%，止损移到**开仓价**
2. 下轨（对侧轨）：**不止盈也不反手**，再减仓50%，止损收紧到**开仓时的中轨**
3. 再有利移动一个步长：减仓50%，止损移到**开仓时的下轨**
4. 此后每继续移动一个步长：减仓50%，止损跟到**上一次减仓价**
5. 一直延续到止损被打出为止；止损只会越收越紧

阶梯步长在ETH价位是8点，BTC和XAU按最近20根收盘中位数相对2,500等比例放大，使三个品种
都落在价格的0.32%附近——8点直接套用到BTC只有0.01%，属于噪音级别。0.32%在125倍杠杆下
正好等于40%账户收益，因此"每8点减仓"和"每40%盈利减仓"是同一条规则的两种说法。

未命中结构的仓位保持原规则：中轨减仓、对侧轨止盈、箱体仍有效时反手。

## 数据库连接

数据库跑在本机 Docker 的 MySQL 8.4，`DATABASE_URL` 指向 `127.0.0.1:3306`。
**不要用 `localhost`**——MySQL 客户端遇到 `localhost` 会走 Unix socket，而库在容器里，
socket 不通，必须走 TCP。

### 为什么不放公网

2026-09-03 把库从公网服务器搬回本机，同样的操作实测：

| | 公网库 | 本地库 |
|---|---|---|
| 往返 | 26.30 ms | 0.36 ms |
| 取 500 根 K 线 | 537 ms | 3.06 ms |
| `load_market_bars` ×15 | 8.69 s | 0.11 s |
| `refresh_market_health` ×15 | 7.68 s | 0.09 s |
| **双行情对照单轮耗时** | **119 s**（最大 202） | **约 9 s** |

瓶颈是公网那台的**下行带宽只有约 140 KB/s**（上行 1.7 MB/s，不对称）。策略每轮要下载
约 1,700 KB 的 K 线，光传输就要 12 秒。搬到本地后单轮从 119 秒降到约 9 秒，
`--poll-seconds` 重新生效——公网时期单轮工作超过 60 秒，循环一直是 `wait(0.1)` 背靠背跑。

### Engine 必须是进程内单例

`build_engine()` 用 `lru_cache` 缓存，**不要改成每次新建**。几十个存储函数都写成
`engine or build_engine()`，每新建一个 Engine 就是一个空连接池，下一次查询要重做 TCP 与
MySQL 认证握手；库在公网时单次握手实测 514 毫秒，一轮要付几十次。旧写法还会漏连接——
被 GC 的连接池不会 `dispose()`，服务端只能中断，`Aborted_clients` 曾累计到三万以上。

配置变更或测试收尾用 `reset_engine()` 释放连接池并清缓存。

## 运维脚本

```bash
.venv/bin/python scripts/backup.py
```

备份影子状态与监管表、轮转超限日志、清理7天前的旧备份，输出到 `var/backups/<时间戳>/`。

```bash
.venv/bin/python scripts/replay_structure_rule.py 2026-09-02
.venv/bin/python scripts/replay_structure_rule.py 2026-09-03 --since 09:00 --arm structure
```

`--since` 只让台账统计该北京时间之后开的仓，之前的行情照跑不误——箱体确认是逐根K线累积
的状态，冷启动直接从关注时刻开跑会让前几笔单子因为箱体尚未确认而消失；预热时长由
`--warmup-hours` 控制，默认12小时。`--arm structure` 只跑当前策略，省掉对照组的一半耗时。

顶底结构规则的对照复盘：同一段行情逐分钟跑两遍影子模拟器，一遍关闭结构判定、一遍开启，
打印两组的交易台账并把明细写到 `var/replay/<日期>/result.json`。必须逐分钟驱动而不是一次性
喂整段行情——`box_active` 是单份可变状态，一次性回放会让分钟循环读到收盘后的箱体结论。
两组输出都不写数据库，也不碰线上的 `.runtime` 状态文件。

## 即时邮件通知

邮件已从固定四小时摘要改为事件触发，不再使用 `STATUS_EMAIL_INTERVAL_HOURS`。

**邮箱只发系统事件，不发行情交易明细。** 事件按 `category` 分流，`TRADE` 类只推钉钉：

| 类别 | 内容 | 邮件 | 钉钉 |
|---|---|---|---|
| `SYSTEM`（默认） | 服务启动、停止；行情接口首次中断与恢复；账户触发或解除熔断；行情健康阻断、策略准入阻断、执行层阻断 | ✅ | ✅ |
| `TRADE` | 逐笔模拟开仓、减仓、部分止盈、平仓、止损 | ❌ | ✅ |

相同持续状态在单次服务运行中只通知一次，连续重试只在首次失败和恢复时通知，避免一分钟
轮询反复刷屏。每封事件邮件附带当时的账户风控、持仓、模拟权益以及Mac的CPU、内存、磁盘和
进程占用；邮件审计不保存正文或SMTP授权码。

要让某类事件重新进邮箱，改 `notifications/runtime_events.py` 里的
`EMAIL_SUPPRESSED_CATEGORIES` 即可，调用点不用动。

## 钉钉机器人通知

事件同时推送到钉钉自定义机器人和邮箱，两条通道互相独立：只配其中一个也能工作，钉钉推送
失败会被吞掉，不影响邮件与策略主流程。配置两个变量后启用，留空则完全禁用：

```bash
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=...
DINGTALK_SECRET=SEC...
```

安全方式使用**加签**而非关键词或IP白名单——家用宽带IP会变，流量经代理时出口IP也会随节点
切换，IP白名单会频繁失效。消息渲染为Markdown并按严重度加标记（🔴止损/熔断、🟠警告、
🟢恢复、🔵一般），手机通知栏一眼可辨。

本地限流为每分钟18条。平台上限是20条，一旦触发会静默该机器人10分钟——而集中爆发止损
的那几分钟恰恰最需要告警，因此宁可本地先丢一条，也不能让整个通道被封。

## Web只读监管后台

后台使用 FastAPI、Jinja2 和 ECharts，采用 Apple-inspired 的系统字体、克制色彩、卡片层级、
44px触控区域和响应式布局，支持macOS深浅色模式。页面包括双账户总览、交互式K线与布林带、
影子交易事件、账户权益和Mac系统状态。Gate与币安按各自实盘公共行情显示，所有页面明确标记
`LIVE_TRADING=false`，Web路由中不存在开仓、平仓、撤单或策略修改接口。

监管页面必须让失败可见，因此所有数据刷新走同一个调度器：页面顶部常驻一行状态，正常时显示
`更新于 HH:MM:SS`，失败时变成红底告警并给出连续失败次数与上次成功时间；轮询用指数退避，
后端异常时不会继续按固定间隔空打。交易记录分页返回（默认每页20条，表格固定高度内滚动、
表头吸顶）。静态资源URL带 `?v=<mtime>` 版本号，改动样式或脚本后浏览器与CDN不会再用旧缓存。

首次使用先创建超级管理员和两套系统实验账户：

```bash
python main.py web-init-admin
```

该命令只在 `admin` 不存在时生成一次性密码，重复执行不会重置密码。`admin` 角色固定为
`SUPER_ADMIN`，不拥有交易账户，只能监管。首次登录必须设置新密码：至少8个字符，且包含大写
字母、小写字母、数字、标点符号中的至少3类；密码使用
scrypt随机盐摘要保存，连续五次失败会锁定十五分钟，登录、退出和密码修改都会写入不含密码的
管理审计表。

启动本机Web服务：

```bash
python main.py web-run
```

默认地址为 `http://127.0.0.1:8765`，只允许Mac本机访问。需要手机远程访问时，应通过
Tailscale等安全内网并显式设置 `WEB_HOST`，不要把该端口直接暴露到公网。Web服务和双行情
模拟服务是两个职责独立的进程：前者只读展示，后者继续采集行情和维护影子账户；关闭Web页面
不会停止模拟交易。

双行情服务每分钟把两套账户的权益、峰值、当日起始权益、持仓数量和风控状态写入
`shadow_equity_snapshots`，并将新开仓、减仓、平仓及风险事件幂等写入
`shadow_trade_events`。当前系统实验账户不属于 `admin`；以后增加普通用户和正式交易账户时，
通过 `trading_accounts.owner_user_id`、交易场所和 `SHADOW/DEMO/LIVE` 环境继续隔离。

## 多用户数据隔离

监管页面的数据按访问者过滤，而不是一律返回全量：

- **超级管理员**看全部账户，包括系统影子账户
- **普通用户**只看 `trading_accounts.owner_user_id` 等于自己的账户；`owner_type=SYSTEM`
  的系统影子账户对其一律不可见
- 角色缺失、大小写不符或近似取值都按最小权限处理，不会被当成超管

`build_overview` 与 `build_trade_events` 的 `viewer` 是**没有默认值的关键字参数**——将来新增
接口若忘记传，会直接报错而不是静默返回所有人的数据。可见性判定 `account_is_visible` 是不触库
的纯函数，与查询分离，因此能独立测试。

接入第三方登录（OAuth2）前必须先完成这一层：隔离缺位时，任何能注册登录的人都能看到全部
持仓、权益曲线和交易明细。

## 旧EMA研究（已废弃，不参与运行）

`backtest-ema` 会从 MySQL 读取 BTC、ETH 的四个周期，使用下一根K线开盘价执行信号，
并计入手续费、滑点和 ATR 止损。该命令只做本地研究，不调用下单接口，也不会改变
`LIVE_TRADING=false`。当前版本尚未模拟交易所强平、资金费率和盘口冲击，因此结果不能
直接作为实盘收益预期。

回测风控以 UTC 00:00 作为每日分界。某根K线收盘确认当日亏损达到 2% 后，系统在
下一根K线开盘平仓并禁止当天再次开仓；累计最大回撤达到 8% 后，同样在下一根K线
开盘平仓，并永久禁止该次策略运行继续开仓。由于使用K线而非逐笔成交数据，跳空、
滑点或单根K线内的快速波动仍可能让最终损失略微越过阈值。

`diagnose-ema` 使用风控生效后的逐笔交易记录，将净盈亏拆成手续费前盈亏、手续费和
估算滑点，同时汇总多空方向、ATR止损次数、平均持仓K线数、最长连续亏损和进场趋势
强度。滑点是按成交名义金额估算的反事实成本，不能当成交易所实际扣费；策略退出和
熔断退出都在下一根K线开盘执行，因此统一归入“开盘退出”。

`research-ema` 固定EMA 20/50/200，只组合研究趋势强度门槛、ATR止损倍数和多空方向。
每组行情严格按时间切成前70%训练集和后30%验证集，参数只由训练集得分选择；验证集
同时运行原始基线和选中参数。验证结果不会反向参与选参，交易少于8笔的训练候选会被
拒绝，避免用不交易制造虚假的低回撤。

`walkforward-ema` 使用三个扩展训练窗口：40%训练/20%验证、60%训练/20%验证、
80%训练/20%验证。三个验证区间按时间连续且互不重叠，每折重新独立选参；最终报告
验证区间复合收益、最差单折回撤、盈利窗口数量以及三折参数是否完全一致。
如果首个训练窗口没有任何候选达到至少8笔交易，命令会标记“样本不足”并继续其他周期，
不会自动降低门槛或把极少数交易当成可靠结论。

`walkforward-filters` 固定EMA 20/50/200与趋势强度门槛0，滚动研究ADX 0/25、已完成
高周期EMA200确认开关、ATR止损后冷却0/5根、ATR 1.5/2.0/2.5以及多空/仅多/仅空，
共72组候选。高周期结果只在高周期K线完整结束后可见，ADX使用上一根已收盘K线，
冷却期按实际ATR止损逐次因果重算。

`walkforward-pullback` 保持EMA 20/50/200，比较原始20/50交叉与EMA顺序已经形成后的
回踩恢复入场。回踩模式要求价格近期触碰EMA20、收盘重新越过EMA20且K线方向与趋势
一致，确认信号统一移动到下一根开盘执行。研究回踩观察窗口2/3/5根、ATR三档和多空/
仅多/仅空，共36组候选。

`walkforward-regime` 不固定押注多头或空头，只比较EMA200相对0/5/10根之前是否已经
持续转向，并组合ATR 1.5/2.0/2.5，共9组候选。价格在EMA上方时本根EMA自然会上升，
因此没有信息增量的“一根斜率”不作为独立过滤器；回看5/10根用于判断更持续的趋势状态。

`walkforward-ema12` 是从外部旧系统抽离出的独立研究策略，只支持15m和30m。它要求
EMA12位于EMA144和EMA169同一侧、最近5根斜率同向、价格真实触碰EMA12后重新离开，
并用已完整收盘的30m或1h EMA排列再次确认方向。旧代码里的固定美元止损、亏损后加仓和
高倍固定仓位全部弃用，继续复用本项目的ATR仓位、交易成本、每日2%及回撤8%熔断。
命令只比较1/2/3根回踩窗口与三档ATR，共9组候选；它不会写入准入表或进入模拟下单。

多周期RSI同样已抽成纯信号模块，保留1h方向、5m回撤、1m极值恢复的原始层级，并强制
高周期收盘后才可见、下一根1m才执行。由于本项目当前明确只运行5m至1h，数据库也没有
足够的1m历史，RSI暂不接入命令、准入和模拟交易；不能用5m代替1m后冒充原策略结果。

`walkforward-breakout` 用EMA20/50和EMA200确定多空趋势，只在收盘价突破此前10/20/40根
K线区间时入场。历史区间明确先移动一根再计算，当前K线不会参与自己的突破门槛；信号
仍在下一根开盘执行。研究固定多空双向，只组合三档突破窗口和ATR 1.5/2.0/2.5，共9组
候选，并沿用手续费、滑点、每日2%及最大回撤8%熔断。该命令只研究，不写准入记录。

`walkforward-joint-breakout` 不允许BTC和ETH分别挑选各自最有利的参数。每一折都用同一组
突破窗口和ATR同时回测两个品种，训练排名首先比较两者中较差的“收益减半倍回撤”得分，
最差得分相同时才比较平均得分。选中的共同参数随后分别进入两个独立样本外窗口；输出按
品种检查复合收益、2/3盈利窗口、8%回撤和每折至少8笔交易，但不会自动写入准入表。

`walkforward-joint-exits` 聚焦共同突破的退出管理：入场窗口只保留20/40根，组合ATR止损
1.5/2.0/2.5、固定止盈关闭/2ATR/3ATR以及固定止损/移动止损，共36组候选。止盈和止损
都使用进场前最后一根已收盘K线的ATR，不读取执行K线未来波动；参数仍按BTC、ETH中较弱
训练得分选择。该功能目前只属于研究回测，纸面交易尚未实现止盈或移动止损执行。

`walkforward-joint-regime` 保持固定止损且不设止盈，要求已完成1小时K线的EMA200方向确认，
再组合ADX 20/25、EMA200斜率回看5/10根、突破窗口20/40根和ATR止损三档，共24组共同
候选。ADX与EMA斜率都只读取执行前已收盘数据，用于过滤弱趋势和横盘阶段。

`walkforward-joint-macro` 使用相同的24组候选，但把30m策略的方向确认提升到已完整收盘的
4小时EMA200。验证集会附带至少约1600根30m预热K线，避免宏观EMA尚未形成时错误漏掉
样本外开仓；它主要用于过滤长期上涨过程中的短周期假空头突破。

`qualify-ema` 是进入模拟交易前的硬门禁。默认要求滚动样本外复合收益严格大于0、
至少2/3验证窗口盈利、最差单折回撤低于8%，并且每个验证窗口至少8笔交易。所有条件
必须同时满足；结论与完整门槛会幂等写入 `strategy_qualifications`。被拒绝的策略不能
启动30天模拟运行，且 `LIVE_TRADING=false` 不受任何准入结果影响。

`qualify-breakout-fixed` 只评估BTC、ETH的30m固定策略：EMA20/50/200趋势、15根区间突破、
ADX至少25、已完成1小时EMA200方向确认、EMA200斜率回看5根、1.5ATR止损、多空双向。
三折中每折只有这一组参数，验证结果不能反向改变参数；通过后写入现有EMA_TREND 1.0.0
准入通道，使当前纸面信号循环可以解析，同样不会启用交易所下单。

执行安全层使用策略运行、准入哈希、品种、周期、K线时间和动作生成确定性的
`client_order_id`。同一订单重试返回原记录；状态不明时只能按该编号查询，禁止直接
重发。任何非零仓位或待处理开仓单都会占用品种名额，禁止补仓。`execution-safety`
只读取本地准入、活动订单和仓位计数；当前Gate客户端仍不提供下单方法。

`risk-snapshot` 只调用Gate测试网账户查询，将Decimal权益、余额、可用保证金、已用保证金
和未实现盈亏写入MySQL，再按UTC日界线计算每日2%与历史峰值回撤8%熔断。订单预留
要求对应运行时状态必须存在且为 `NORMAL`；状态缺失同样拒绝开仓。每日熔断保持到UTC
次日，总回撤熔断必须人工审核，不能由权益短暂反弹自动解除。

`market-health` 为BTC、ETH每个K线周期记录独立心跳，检查数据库最新已收盘K线是否超过
两个周期未更新、时间是否异常以及连续失败次数。订单预留会再次检查心跳是否在120秒
以内，并要求最新K线时间覆盖订单信号；缺失、STALE、TIMEOUT和ERROR全部禁止开仓。

`market-runner` 每60秒为BTC、ETH的5m/15m/30m/1h周期刷新最近K线；长期运行建议每轮取
500根，然后更新
每条行情流的健康状态。每轮都会关闭Gate测试网连接，网络或数据库异常后按5、10、20秒
逐步退避并在300秒封顶，下一轮重新连接。SIGINT、SIGTERM和Docker停止信号会先结束当前
请求，再保存STOPPED状态。`.runtime` 文件锁防止本机重复启动；运行心跳、PID、成功轮数、
连续失败和脱敏错误保存在 `service_runtime_states`。该服务只读取Gate测试网行情，不包含
下单方法，`LIVE_TRADING` 始终为false。

`paper-signal-cycle` 分别读取ETH最近的5分钟和15分钟已收盘K线，独立运行两个同周期判断，
并把动作时间设置为各自周期的下一根K线开盘。信号使用新策略版本、品种、周期、执行时间、方向
和动作生成SHA-256唯一键，重复运行不会重复写入。信号审计与订单准入分离：没有新策略
APPROVED记录时信号仍可保存用于观察，但订单数始终为0。
长期观察时可使用 `market-runner --limit 500 --enable-signals`，每轮行情完整写入并通过健康
检查后才执行信号周期；任何信号周期异常都会进入同一套退避重试和服务心跳记录。

当前首轮布林带三折验证结论为REJECTED，因此订单路径保持关闭。即使未来回测达到APPROVED，
在中轨分批止盈和真实保本止损执行层完成独立验证前，系统仍有第二道
`BLOCKED_EXECUTION_NOT_READY`硬门禁。任何情况下都没有提交到交易所的代码。

每轮信号处理前会优先监控非零模拟仓位。程序按时间扫描上次检查后的已收盘K线；价格触及
ATR保护止损时创建只减仓本地订单，跳空越过止损会使用更差的开盘价，再叠加不利滑点和
手续费。未触发时更新盯市价和未实现盈亏。EMA退出信号同样先于反向开仓处理；成交、订单、
持仓和止损游标在同一个MySQL事务中提交，确定性成交编号保证崩溃重试不会重复成交。

本地模拟账户使用独立 `paper_account_states` 账本，并且只在首个已批准开仓信号到达时固定
初始资金，避免把策略观察期误计入至少30天的正式模拟期。余额等于初始资金加已实现毛盈亏
再减全部手续费，权益再加当前未实现盈亏；持仓盯市或成交后都会生成账户快照，并使用
`GATE_TESTNET_PAPER` 独立计算每日2%和历史峰值回撤8%熔断。开仓入口读取模拟风控状态，
Gate测试账户自身的NORMAL状态不能覆盖模拟账户已经触发的熔断。

OANDA黄金使用同一套本地订单、成交、持仓和保护止损机制，但资金和风控场所隔离为
`OANDA_PRACTICE_PAPER`，数量单位记录为 `OANDA_UNITS`。默认仅30m为主执行周期，15m和
1h即使同时运行也只记录影子信号，禁止多周期争抢同一黄金仓位。仓位按账户返回的
`tradeUnitsPrecision`、最小/最大单位、价格精度和 `marginRate` 计算，系统配置的125倍只
是上限，不能覆盖OANDA实际保证金限制。只有 `oanda-runner --intervals 30m --enable-signals`
持续运行且账户级 `XAU_USD` 规则与报价检查通过后，才会初始化黄金模拟本金和30天监督；
检查失败会明确显示 `BLOCKED_ACCOUNT_INSTRUMENT`，不会猜测交易规则或启动计时。

### 逆结构开仓否决

开仓前检查生效结构，方向相反即跳过这一单：有顶部结构不做多、有底部结构不做空。首次触轨
开仓和对侧轨止盈后的反手都适用。检测范围与阶梯延续一致——全部七个周期。

逐分钟对照回放 2026-09-02 11:01 至 09-03 07:50（北京时间）：关闭结构规则 29 笔、净
-38.88 USDT；开启后 17 笔、净 -7.60 USDT，两组胜率都是 50%。收益差主要来自阶梯延续多吃
的那几段趋势，而不是开仓否决本身。

按命中周期拆开看，被否决的 13 笔里：3m/5m 命中的 4 笔净值 +49.73（拦下 3 笔止损、误杀 1 笔
盈利），30m/1h 命中的 6 笔净值 -13.72（拦下 3 笔止损、误杀 3 笔各约 +20 的盈利单）。长周期
结构一旦形成会持续数小时，等于对该方向全时段封禁，短周期结构则更贴近当下这一波。样本只有
一个交易日，这个拆分只说明方向，不构成参数结论。
