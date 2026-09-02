"""数据库核心表和幂等约束测试。"""

from gold_crypto_quant.storage.base import Base


def test_core_table_names_are_registered() -> None:
    # 导入 models 会执行类声明，并把所有表注册到 Base.metadata。
    from gold_crypto_quant.storage import models  # noqa: F401

    assert set(Base.metadata.tables) == {
        "account_snapshots",
        "admin_audit_logs",
        "app_users",
        "email_delivery_logs",
        "instruments",
        "market_bars",
        "market_data_health_states",
        "orders",
        "paper_account_states",
        "paper_simulation_daily_metrics",
        "paper_simulation_states",
        "paper_position_controls",
        "positions",
        "risk_events",
        "runtime_risk_states",
        "service_runtime_states",
        "service_heartbeats",
        "shadow_equity_snapshots",
        "shadow_trade_events",
        "strategy_runs",
        "strategy_qualifications",
        "strategy_signals",
        "trades",
        "trading_accounts",
    }


def test_deduplication_constraints_exist() -> None:
    # 再次导入不会重复注册；Python 模块缓存保证声明只执行一次。
    from gold_crypto_quant.storage import models  # noqa: F401

    # 信号和订单的唯一键是断线重试时防止重复执行的最后一道保护。
    assert Base.metadata.tables["strategy_signals"].c.dedupe_key.unique
    assert Base.metadata.tables["orders"].c.client_order_id.unique


def test_every_table_and_column_has_chinese_description() -> None:
    # 导入模型以注册注释，然后遍历全部元数据，防止新增字段时遗漏数据库说明。
    from gold_crypto_quant.storage import models  # noqa: F401

    for table in Base.metadata.tables.values():
        assert table.comment, f"表 {table.name} 缺少中文说明"
        for column in table.columns:
            assert column.comment, f"字段 {table.name}.{column.name} 缺少中文说明"
