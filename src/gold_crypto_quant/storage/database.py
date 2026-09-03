"""MySQL 连接、健康检查和表结构初始化。"""

from functools import lru_cache

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.schema import CreateColumn

from gold_crypto_quant.config import get_settings
from gold_crypto_quant.storage.base import Base


@lru_cache
def build_engine() -> Engine:
    """返回进程内唯一的 SQLAlchemy Engine。

    ``pool_pre_ping`` 会在复用连接前检查其是否存活；``pool_recycle`` 避免连接因
    MySQL 超时被服务端关闭后仍留在连接池中。

    这里必须缓存：几十个存储函数都写成 ``engine or build_engine()``，每次新建
    Engine 就等于新建一个空连接池，下一次查询要重做 TCP 与 MySQL 认证握手。
    数据库在公网时单次握手实测约 514 毫秒，一轮行情对照要付几十次，
    是轮询间隔从60秒涨到119秒的主因。Engine 本身线程安全，共享没有风险。
    """
    # get_settings() 从 .env 取得连接地址；密码不会出现在调用方代码中。
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def reset_engine() -> None:
    """丢弃缓存的 Engine 并关闭其连接池；配置变更或测试收尾时调用。"""
    cached = build_engine.cache_info().currsize
    if cached:
        build_engine().dispose()
    build_engine.cache_clear()


def check_connection(engine: Engine | None = None) -> bool:
    """执行只读 ``SELECT 1``，确认数据库和账号权限可用。"""
    # 测试可传入隔离 Engine；正常运行时才创建项目默认 Engine。
    engine = engine or build_engine()
    # 上下文结束后连接自动归还连接池，即使查询异常也不会泄漏连接。
    with engine.connect() as connection:
        # scalar_one() 明确要求查询只返回一个标量，异常结果会立即暴露。
        return connection.execute(text("SELECT 1")).scalar_one() == 1


def create_schema(engine: Engine | None = None) -> None:
    """创建尚不存在的核心表，不删除或覆盖现有表和数据。"""
    # 必须先导入模型，SQLAlchemy 才会把全部表注册到 Base.metadata。
    from gold_crypto_quant.storage import models  # noqa: F401

    # 允许测试传入临时 Engine；命令行调用时使用 .env 中的 MySQL。
    engine = engine or build_engine()
    # create_all 只创建缺失表，可重复调用；正式结构升级后将改用迁移工具。
    Base.metadata.create_all(engine)


def sync_schema_comments(engine: Engine | None = None) -> None:
    """把模型中的中英文说明同步到现有 MySQL 表和字段。

    MySQL 修改字段注释时要求重新声明完整字段定义，所以这里使用 SQLAlchemy 按当前模型
    编译字段定义，并显式要求 ``ALGORITHM=INPLACE`` 与 ``LOCK=NONE``。同步前还会确认
    目标表和字段全部存在，发现结构不一致时直接停止，避免在漂移的数据库上盲目修改。
    """
    # 导入模型后，表注释和字段注释才会注册到 Base.metadata。
    from gold_crypto_quant.storage import models  # noqa: F401

    engine = engine or build_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)
    missing_tables = expected_tables - existing_tables
    if missing_tables:
        raise RuntimeError(f"database is missing tables: {sorted(missing_tables)}")

    # 逐表确认字段集合，防止 MODIFY COLUMN 意外覆盖数据库中的手工结构变更。
    for table in Base.metadata.sorted_tables:
        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        expected_columns = {column.name for column in table.columns}
        if existing_columns != expected_columns:
            raise RuntimeError(f"column mismatch in table {table.name}")

    preparer = engine.dialect.identifier_preparer
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            quoted_table = preparer.quote(table.name)
            # 表名来自受控模型，表注释通过参数绑定，避免字符串拼接产生转义问题。
            connection.execute(
                text(f"ALTER TABLE {quoted_table} COMMENT = :table_comment"),
                {"table_comment": table.comment or ""},
            )
            for column in table.columns:
                # CreateColumn 会保留类型、NULL、默认值和 AUTO_INCREMENT，仅增加 COMMENT。
                definition = str(CreateColumn(column).compile(dialect=engine.dialect))
                connection.exec_driver_sql(
                    f"ALTER TABLE {quoted_table} MODIFY COLUMN {definition}, "
                    "ALGORITHM=INPLACE, LOCK=NONE"
                )
