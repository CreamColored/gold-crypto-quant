"""Engine 进程内单例测试。"""

from gold_crypto_quant.storage.database import build_engine, reset_engine


def test_build_engine_is_cached_per_process() -> None:
    """必须复用同一个 Engine：每次新建等于新建空连接池，下一次查询要重做握手。

    数据库在公网时单次握手实测约514毫秒，而几十个存储函数都写成
    ``engine or build_engine()``，一轮行情对照要付几十次。
    """
    reset_engine()
    first = build_engine()
    try:
        assert build_engine() is first
        assert build_engine().pool is first.pool
    finally:
        reset_engine()


def test_reset_engine_drops_the_cached_instance() -> None:
    """重置后必须换一个新 Engine，否则配置变更无法生效。"""
    reset_engine()
    first = build_engine()
    reset_engine()
    second = build_engine()
    try:
        assert second is not first
    finally:
        reset_engine()
