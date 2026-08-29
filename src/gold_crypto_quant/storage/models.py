"""量化系统的 MySQL 核心数据模型。

设计约定：

* 所有业务时间统一保存为 UTC，并使用微秒精度 DATETIME(6)。
* 价格、数量和盈亏使用 DECIMAL，禁止用二进制浮点数保存财务数据。
* 信号和订单都包含唯一业务键，用数据库约束兜底防止重复执行。
* 原始接口响应可写入 JSON，关键查询字段仍拆成普通列并建立索引。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME as DateTime
from sqlalchemy.orm import Mapped, mapped_column

from gold_crypto_quant.storage.base import Base

# 统一由 MySQL 生成创建时间，避免不同应用进程的本地时钟产生偏差。
CREATED_AT = text("CURRENT_TIMESTAMP(6)")


class Instrument(Base):
    """可交易品种，例如 OKX 的 BTC-USDT-SWAP 或 OANDA 的 XAU_USD。"""

    __tablename__ = "instruments"
    # 同一交易场所的品种代码必须唯一，防止行情和订单关联到重复品种。
    __table_args__ = (
        UniqueConstraint("venue", "symbol", name="uq_instrument_venue_symbol"),
        {"comment": "交易品种主数据，记录交易场所、品种代码和资产分类"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="品种主键ID"
    )
    venue: Mapped[str] = mapped_column(String(32), nullable=False, comment="交易场所代码")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="交易品种代码")
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, comment="资产类别")
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False, comment="基础资产代码")
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False, comment="计价资产代码")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1"), comment="是否启用该品种"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT, comment="记录创建时间UTC"
    )


class MarketBar(Base):
    """标准化 OHLCV K 线；一条记录只代表一个已经识别的品种与周期。"""

    __tablename__ = "market_bars"
    # 唯一约束支持幂等写入；组合索引优化按品种、周期和时间范围读取。
    __table_args__ = (
        UniqueConstraint("instrument_id", "interval_code", "open_time", name="uq_market_bar"),
        Index("ix_market_bar_lookup", "instrument_id", "interval_code", "open_time"),
        {"comment": "标准化K线行情，按品种、周期和开盘时间唯一存储"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="K线记录主键ID"
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, comment="关联品种ID"
    )
    interval_code: Mapped[str] = mapped_column(String(8), nullable=False, comment="K线周期代码")
    open_time: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="K线开盘时间UTC"
    )
    close_time: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="K线收盘时间UTC"
    )
    open_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, comment="开盘价")
    high_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, comment="最高价")
    low_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, comment="最低价")
    close_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, comment="收盘价")
    volume: Mapped[Decimal] = mapped_column(
        Numeric(36, 12), nullable=False, comment="基础成交量或合约张数"
    )
    quote_volume: Mapped[Decimal | None] = mapped_column(Numeric(36, 12), comment="计价资产成交额")
    trade_count: Mapped[int | None] = mapped_column(Integer, comment="成交笔数")
    is_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1"), comment="K线是否已经收盘确认"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT, comment="记录创建时间UTC"
    )


class StrategyRun(Base):
    """一次回测或模拟运行，保存策略版本、周期和完整参数。"""

    __tablename__ = "strategy_runs"
    __table_args__ = {"comment": "策略运行记录，保存回测或模拟运行的参数与状态"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="策略运行UUID")
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="策略名称")
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="策略版本")
    interval_code: Mapped[str] = mapped_column(String(8), nullable=False, comment="运行周期代码")
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="运行状态")
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="本次运行的完整策略参数"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="运行开始时间UTC"
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(fsp=6), comment="运行结束时间UTC")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT, comment="记录创建时间UTC"
    )


class StrategyQualification(Base):
    """策略进入模拟交易前的样本外准入审计记录。"""

    __tablename__ = "strategy_qualifications"
    __table_args__ = (
        Index("ix_qualification_strategy_time", "strategy_name", "evaluated_at"),
        {"comment": "策略模拟交易准入审计，保存样本外指标、门槛和批准或拒绝结论"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="策略准入记录主键ID"
    )
    evaluation_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="准入评估内容幂等哈希"
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, comment="关联品种ID"
    )
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="策略名称")
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="策略版本")
    interval_code: Mapped[str] = mapped_column(String(8), nullable=False, comment="评估K线周期")
    decision: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="准入结论APPROVED或REJECTED"
    )
    compounded_return: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False, comment="滚动样本外复合收益率"
    )
    worst_drawdown: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False, comment="滚动样本外最差单折回撤率"
    )
    positive_folds: Mapped[int] = mapped_column(Integer, nullable=False, comment="盈利验证窗口数量")
    total_folds: Mapped[int] = mapped_column(Integer, nullable=False, comment="验证窗口总数量")
    minimum_fold_trades: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="单个验证窗口最少交易笔数"
    )
    criteria: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="本次准入使用的完整门槛"
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="每个验证窗口指标和选中参数"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="批准或拒绝原因说明")
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="准入评估时间UTC"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT, comment="记录创建时间UTC"
    )


class StrategySignal(Base):
    """策略生成的进出场信号，不代表订单一定已经提交。"""

    __tablename__ = "strategy_signals"
    __table_args__ = (
        Index("ix_signal_instrument_time", "instrument_id", "bar_time"),
        {"comment": "策略信号记录，保存进出场判断并通过唯一键防止重复"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="信号记录主键ID"
    )
    # 调用方根据策略、品种、周期、K线时间和动作生成哈希，确保信号只写一次。
    dedupe_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="信号幂等去重键"
    )
    strategy_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_runs.id", ondelete="SET NULL"), comment="关联策略运行UUID"
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, comment="关联品种ID"
    )
    interval_code: Mapped[str] = mapped_column(String(8), nullable=False, comment="信号周期代码")
    bar_time: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="产生信号的K线时间UTC"
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False, comment="信号方向LONG或SHORT")
    signal_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="信号类型ENTRY或EXIT"
    )
    signal_price: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="信号确认价格"
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="指标值及其他信号上下文")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT, comment="记录创建时间UTC"
    )


class Order(Base):
    """提交给模拟交易所的委托及其最新状态。"""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_order_instrument_time", "instrument_id", "submitted_at"),
        {"comment": "模拟交易委托订单，记录请求参数、交易所编号和最新状态"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="订单记录主键ID"
    )
    # client_order_id 是订单幂等键；程序重试时必须复用原值，禁止重复开仓。
    client_order_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="客户端订单幂等编号"
    )
    venue_order_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, comment="交易所返回的订单编号"
    )
    strategy_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_runs.id", ondelete="SET NULL"), comment="关联策略运行UUID"
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, comment="关联品种ID"
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False, comment="买卖方向BUY或SELL")
    position_side: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="持仓方向LONG或SHORT"
    )
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="订单类型")
    status: Mapped[str] = mapped_column(String(24), nullable=False, comment="订单最新状态")
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 12), nullable=False, comment="委托数量")
    price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), comment="委托价格")
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), comment="触发或止损价格")
    # 平仓单应设置 reduce_only，避免反向成交后意外建立新仓位。
    reduce_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0"), comment="是否为只减仓订单"
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="订单提交时间UTC"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6),
        nullable=False,
        server_default=CREATED_AT,
        onupdate=datetime.utcnow,
        comment="订单最后更新时间UTC",
    )
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="交易所原始订单响应")


class Trade(Base):
    """订单产生的实际成交；一个订单可能对应多笔成交。"""

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("venue", "venue_trade_id", name="uq_trade_venue_id"),
        Index("ix_trade_instrument_time", "instrument_id", "executed_at"),
        {"comment": "订单实际成交明细，一张订单可以对应多笔成交"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="成交记录主键ID"
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, comment="关联订单ID"
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, comment="关联品种ID"
    )
    venue: Mapped[str] = mapped_column(String(32), nullable=False, comment="交易场所代码")
    venue_trade_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="交易所成交编号"
    )
    price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, comment="成交价格")
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 12), nullable=False, comment="成交数量")
    fee: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, comment="成交手续费")
    fee_asset: Mapped[str] = mapped_column(String(16), nullable=False, comment="手续费资产代码")
    realized_pnl: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 12), comment="本次成交已实现盈亏"
    )
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="成交时间UTC"
    )
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="交易所原始成交响应")


class Position(Base):
    """每个交易场所、品种和持仓方向的当前聚合仓位。"""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("venue", "instrument_id", "position_side", name="uq_open_position"),
        {"comment": "当前聚合持仓，按交易场所、品种和持仓方向唯一"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="持仓记录主键ID"
    )
    venue: Mapped[str] = mapped_column(String(32), nullable=False, comment="交易场所代码")
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, comment="关联品种ID"
    )
    position_side: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="持仓方向LONG或SHORT"
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(36, 12), nullable=False, comment="当前持仓数量"
    )
    entry_price: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="持仓平均开仓价"
    )
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), comment="最新标记价格")
    # 保存交易所最终确认的实际杠杆，不直接假设配置的 125 倍一定可用。
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, comment="实际使用杠杆倍数")
    liquidation_price: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 12), comment="预计强平价格"
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, server_default=text("0"), comment="当前未实现盈亏"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6),
        nullable=False,
        server_default=CREATED_AT,
        onupdate=datetime.utcnow,
        comment="持仓最后更新时间UTC",
    )


class PaperPositionControl(Base):
    """本地模拟仓位的策略周期、合约乘数和保护止损。"""

    __tablename__ = "paper_position_controls"
    __table_args__ = {"comment": "本地模拟仓位控制信息，保存入场来源、保护止损和退出原因"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="模拟仓位控制主键ID"
    )
    position_id: Mapped[int] = mapped_column(
        ForeignKey("positions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        comment="关联当前聚合持仓ID",
    )
    entry_order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, comment="本轮入场订单ID"
    )
    strategy_run_id: Mapped[str] = mapped_column(
        ForeignKey("strategy_runs.id", ondelete="RESTRICT"),
        nullable=False,
        comment="关联模拟策略运行UUID",
    )
    interval_code: Mapped[str] = mapped_column(String(8), nullable=False, comment="策略K线周期")
    contract_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="一张合约对应的标的资产数量"
    )
    protective_stop_price: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="当前ATR保护止损价格"
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="模拟仓位开仓时间UTC"
    )
    last_evaluated_bar_time: Mapped[datetime | None] = mapped_column(
        DateTime(fsp=6), comment="最近完成止损检查的K线开盘时间UTC"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(fsp=6), comment="模拟仓位平仓时间UTC"
    )
    exit_reason: Mapped[str | None] = mapped_column(String(32), comment="最终退出原因")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6),
        nullable=False,
        server_default=CREATED_AT,
        onupdate=datetime.utcnow,
        comment="模拟仓位控制最后更新时间UTC",
    )


class AccountSnapshot(Base):
    """定时记录账户余额、权益和保证金，用于回撤与每日熔断计算。"""

    __tablename__ = "account_snapshots"
    __table_args__ = (
        UniqueConstraint("venue", "snapshot_time", name="uq_account_snapshot"),
        {"comment": "账户资金定时快照，用于收益、每日亏损和最大回撤计算"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="账户快照主键ID"
    )
    venue: Mapped[str] = mapped_column(String(32), nullable=False, comment="交易场所代码")
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="快照时间UTC"
    )
    balance: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="账户钱包余额"
    )
    equity: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, comment="账户权益")
    available_margin: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="可用保证金"
    )
    used_margin: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="已占用保证金"
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="账户未实现盈亏"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT, comment="记录创建时间UTC"
    )


class PaperAccountState(Base):
    """本地模拟账户的固定初始资金和最新权益账本。"""

    __tablename__ = "paper_account_states"
    __table_args__ = {"comment": "本地模拟资金账本，汇总成交手续费、盈亏、保证金和可用资金"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="模拟资金账本主键ID"
    )
    venue: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, comment="模拟交易场所代码"
    )
    initial_equity: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="模拟运行固定初始权益"
    )
    balance: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="扣除手续费并计入已实现盈亏后的余额"
    )
    equity: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="余额加当前未实现盈亏后的模拟权益"
    )
    available_margin: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="扣除占用保证金后的可用保证金"
    )
    used_margin: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="当前模拟持仓占用保证金"
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="累计已实现毛盈亏"
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="当前持仓未实现盈亏"
    )
    total_fees: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="累计模拟成交手续费"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6),
        nullable=False,
        server_default=CREATED_AT,
        onupdate=datetime.utcnow,
        comment="模拟资金账本最后更新时间UTC",
    )


class RiskEvent(Base):
    """记录拒单、每日亏损熔断、最大回撤熔断等风控事件。"""

    __tablename__ = "risk_events"
    __table_args__ = (
        Index("ix_risk_event_time", "event_time"),
        {"comment": "风控事件记录，包括拒单、亏损熔断和最大回撤熔断"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="风控事件主键ID"
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"), comment="关联品种ID，可为空"
    )
    event_time: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="事件发生时间UTC"
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False, comment="风险严重级别")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="风控事件类型")
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="触发原因说明")
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="风控计算明细")
    is_resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0"), comment="事件是否已经处理"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT, comment="记录创建时间UTC"
    )


class RuntimeRiskState(Base):
    """账户级运行时风控状态，订单入口必须读取并遵守。"""

    __tablename__ = "runtime_risk_states"
    __table_args__ = {"comment": "账户运行时风控状态，持久化每日亏损与最大回撤熔断"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="运行时风控状态主键ID"
    )
    venue: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, comment="交易场所代码"
    )
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="风控状态NORMAL、DAILY_HALTED或DRAWDOWN_HALTED"
    )
    trading_day: Mapped[date] = mapped_column(
        Date, nullable=False, comment="按UTC划分的当前交易日期"
    )
    day_start_equity: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="当前UTC交易日的起始权益"
    )
    peak_equity: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="账户历史最高权益"
    )
    latest_equity: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="最近一次账户快照权益"
    )
    daily_return: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False, comment="相对当日起始权益的收益率"
    )
    drawdown: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False, comment="相对历史最高权益的回撤率"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="当前风控状态原因")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6),
        nullable=False,
        server_default=CREATED_AT,
        onupdate=datetime.utcnow,
        comment="风控状态最后更新时间UTC",
    )


class MarketDataHealthState(Base):
    """每个品种周期的行情心跳和数据新鲜度状态。"""

    __tablename__ = "market_data_health_states"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "interval_code",
            name="uq_market_data_health_instrument_interval",
        ),
        {"comment": "行情流健康状态，记录心跳、最新K线和断线熔断原因"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="行情健康状态主键ID"
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False, comment="关联品种ID"
    )
    interval_code: Mapped[str] = mapped_column(String(8), nullable=False, comment="行情K线周期")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="健康状态HEALTHY、STALE、TIMEOUT或ERROR"
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="最近一次健康检查时间UTC"
    )
    last_bar_close_time: Mapped[datetime | None] = mapped_column(
        DateTime(fsp=6), comment="数据库中最新已收盘K线的收盘时间UTC"
    )
    stale_seconds: Mapped[int | None] = mapped_column(
        Integer, comment="检查时行情落后当前时间的秒数"
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="连续不健康检查次数",
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="当前健康状态原因")
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="健康检查详细指标")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6),
        nullable=False,
        server_default=CREATED_AT,
        onupdate=datetime.utcnow,
        comment="行情健康状态最后更新时间UTC",
    )


class ServiceRuntimeState(Base):
    """长期运行服务的心跳、重试和安全退出状态。"""

    __tablename__ = "service_runtime_states"
    __table_args__ = {"comment": "长期运行服务状态，记录进程心跳、重试次数和退出原因"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="服务运行状态主键ID"
    )
    service_name: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="服务唯一名称"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="服务状态STARTING、RUNNING、RETRYING或STOPPED"
    )
    process_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="操作系统进程ID")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="本次服务启动时间UTC"
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="最近一次服务心跳时间UTC"
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(fsp=6), comment="本次服务停止时间UTC"
    )
    successful_cycles: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="本次启动成功完成的轮询次数"
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="当前连续失败次数"
    )
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近一次异常的脱敏摘要")
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="服务周期和品种等运行参数")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6),
        nullable=False,
        server_default=CREATED_AT,
        onupdate=datetime.utcnow,
        comment="服务状态最后更新时间UTC",
    )


class ServiceHeartbeat(Base):
    """长期服务的追加式心跳历史，用于审计断线、重试和运行连续性。"""

    __tablename__ = "service_heartbeats"
    __table_args__ = (
        Index("ix_service_heartbeat_lookup", "service_name", "recorded_at"),
        {"comment": "长期服务心跳历史，保留每次运行、重试和停止事件用于稳定性审计"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="服务心跳主键ID"
    )
    service_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="服务唯一名称"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="心跳时服务状态STARTING、RUNNING、RETRYING或STOPPED"
    )
    process_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="操作系统进程ID")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="本次心跳记录时间UTC"
    )
    successful_cycles: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本进程截至本次心跳的成功轮询次数"
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本次心跳对应的连续失败次数"
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="服务周期、品种和脱敏异常等运行明细"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT, comment="记录创建时间UTC"
    )


class PaperSimulationDailyMetric(Base):
    """模拟运行的UTC每日权益、回撤和服务连续性汇总。"""

    __tablename__ = "paper_simulation_daily_metrics"
    __table_args__ = (
        UniqueConstraint("venue", "trading_day", name="uq_paper_simulation_daily_metric"),
        {"comment": "模拟运行每日稳定性指标，汇总权益、回撤、心跳缺口和重试次数"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="模拟运行每日指标主键ID"
    )
    venue: Mapped[str] = mapped_column(String(32), nullable=False, comment="模拟交易场所代码")
    trading_day: Mapped[date] = mapped_column(Date, nullable=False, comment="UTC交易日期")
    day_start_equity: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="当日起始模拟权益"
    )
    day_end_equity: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="当日结束模拟权益"
    )
    daily_return: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False, comment="当日模拟收益率"
    )
    maximum_drawdown: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False, comment="截至当日的模拟账户最大回撤率"
    )
    heartbeat_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="当日记录的服务心跳数量"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="当日进入RETRYING状态的次数"
    )
    maximum_heartbeat_gap_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="当日相邻服务心跳的最大间隔秒数"
    )
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="当日模拟成交笔数")
    risk_state: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="当日结束时账户风控状态"
    )
    is_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否为完整UTC运行日"
    )
    is_healthy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="该日是否满足稳定运行标准"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="每日健康结论和未通过原因")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT,
        onupdate=datetime.utcnow, comment="每日指标最后更新时间UTC"
    )


class PaperSimulationState(Base):
    """连续30天模拟运行监督状态，永远不直接开启真实交易。"""

    __tablename__ = "paper_simulation_states"
    __table_args__ = {"comment": "模拟运行30天监督总状态，仅提供人工复核依据且不启用真实交易"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="模拟运行监督状态主键ID"
    )
    venue: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, comment="模拟交易场所代码"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="监督状态RUNNING、REVIEW_READY或RISK_FAILED"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="模拟稳定性监督开始时间UTC"
    )
    required_days: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="要求的连续健康天数"
    )
    elapsed_days: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="已经完成的完整UTC运行日数"
    )
    consecutive_healthy_days: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="截至最近完整日的连续健康天数"
    )
    current_equity: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, comment="最近一次模拟账户权益"
    )
    total_return: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False, comment="相对模拟初始权益的累计收益率"
    )
    maximum_drawdown: Mapped[Decimal] = mapped_column(
        Numeric(18, 10), nullable=False, comment="模拟期间最大回撤率"
    )
    service_incident_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="模拟期间服务重试和不健康日累计数量"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="当前监督结论说明")
    last_evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, comment="最近一次监督评估时间UTC"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(fsp=6), nullable=False, server_default=CREATED_AT,
        onupdate=datetime.utcnow, comment="监督状态最后更新时间UTC"
    )
