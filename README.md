# Gold Crypto Quant

加密货币合约的量化研究及模拟交易系统。当前阶段只运行Gate测试网的BTC与ETH，
黄金/OANDA代码和历史数据保留但默认暂停；所有交易仍只允许测试环境，
`LIVE_TRADING` 必须保持为 `false`。

技术栈：Python 3.12、Pandas、vectorbt、MySQL 8、SQLAlchemy 和 PyMySQL。

## 第一阶段规则

- 当前启用品种：BTC/USDT、ETH/USDT
- 周期：5m、15m、30m、1h
- 策略：EMA 20/50 交叉，EMA 200 过滤大趋势
- 方向：多空双向
- 加密货币：官方合约测试网
- 黄金：暂缓，`OANDA_ENABLED=false`
- 杠杆上限：125 倍；禁止补仓
- 单笔账户风险：0.25%
- 每日亏损熔断：2%
- 最大回撤熔断：8%
- 稳定模拟运行至少 30 天后才评估下一阶段

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
python main.py import-gate-bars --limit 1000
python main.py import-gate-bars --history-days 90 --limit 2000
python main.py backtest-ema
python main.py diagnose-ema
python main.py research-ema --intervals 15m 30m 1h
python main.py walkforward-ema --intervals 15m 30m 1h
python main.py walkforward-filters --intervals 15m 30m
python main.py walkforward-pullback --intervals 15m 30m
python main.py walkforward-regime --intervals 5m 15m 30m 1h
python main.py walkforward-ema12 --intervals 15m 30m
python main.py qualify-ema --intervals 15m 30m
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

数据库连接使用 `mysql+pymysql`，字符集统一为 `utf8mb4`，应用和数据库时间统一使用
UTC。密码等敏感配置只写入本地 `.env`，不要提交到版本库。

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

策略信号只根据已收盘 K 线形成，并延迟到下一根 K 线执行，避免未来数据。
125 倍仅为保证金杠杆上限，仓位由账户风险和止损距离决定。

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

`qualify-ema` 是进入模拟交易前的硬门禁。默认要求滚动样本外复合收益严格大于0、
至少2/3验证窗口盈利、最差单折回撤低于8%，并且每个验证窗口至少8笔交易。所有条件
必须同时满足；结论与完整门槛会幂等写入 `strategy_qualifications`。被拒绝的策略不能
启动30天模拟运行，且 `LIVE_TRADING=false` 不受任何准入结果影响。

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

`paper-signal-cycle` 从每条健康行情流读取最近500根已收盘K线，用EMA 20/50/200判断最新
收盘是否产生动作，并把动作时间设置为下一根K线开盘。信号使用策略版本、品种、周期、
执行时间、方向和动作生成SHA-256唯一键，重复运行不会重复写入。信号审计与订单准入分离：
没有APPROVED记录时信号仍可保存用于观察，但订单数始终为0。
长期观察时可使用 `market-runner --limit 500 --enable-signals`，每轮行情完整写入并通过健康
检查后才执行信号周期；任何信号周期异常都会进入同一套退避重试和服务心跳记录。

当且仅当最新准入记录为APPROVED、各滚动窗口参数完全一致、账户风控为NORMAL且行情健康
时，信号周期才会继续构造本地模拟订单。止损使用最新已收盘K线的Wilder ATR；数量先按
账户权益0.25%风险计算，再依据Gate `quanto_multiplier` 换成合约张数并向下取整，同时受
品种最小/最大张数、实际最大杠杆和可用保证金限制。订单先以CREATED状态通过全部门禁，
再原子写入FILLED状态、本地成交、聚合持仓和模拟仓位控制；数量单位明确记录为
GATE_CONTRACTS，仍没有任何提交到交易所的代码。Gate启用小数张的品种和依赖止损后冷却
期的参数目前保持失败关闭。

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
