"""行情心跳与K线新鲜度状态机测试。"""

from datetime import UTC, datetime, timedelta

from gold_crypto_quant.risk.market_health import MarketHealthStatus, evaluate_market_health

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def test_recent_closed_bar_is_healthy() -> None:
    """最新K线在两个周期以内且心跳正常时允许订单继续检查。"""
    # 调用健康状态机，5分钟K线只落后5分钟应为健康。
    result = evaluate_market_health(
        now=NOW,
        interval="5m",
        last_bar_close_time=NOW - timedelta(minutes=5),
        last_heartbeat_at=NOW - timedelta(seconds=30),
    )
    assert result.status == MarketHealthStatus.HEALTHY


def test_bar_older_than_two_intervals_is_stale() -> None:
    """行情超过两个周期未更新必须触发STALE。"""
    # 调用健康状态机，5分钟流落后11分钟已经超过10分钟限制。
    result = evaluate_market_health(
        now=NOW,
        interval="5m",
        last_bar_close_time=NOW - timedelta(minutes=11),
    )
    assert result.status == MarketHealthStatus.STALE


def test_old_heartbeat_times_out_even_if_bar_is_recent() -> None:
    """进程停止心跳时不能仅凭最近K线继续创建订单。"""
    # 调用状态机并模拟121秒没有健康检查，优先返回TIMEOUT。
    result = evaluate_market_health(
        now=NOW,
        interval="15m",
        last_bar_close_time=NOW - timedelta(minutes=15),
        last_heartbeat_at=NOW - timedelta(seconds=121),
    )
    assert result.status == MarketHealthStatus.TIMEOUT


def test_future_bar_time_is_error() -> None:
    """数据库K线时间晚于系统时间说明时钟或数据异常。"""
    # 调用健康状态机并传入未来收盘时间。
    result = evaluate_market_health(
        now=NOW,
        interval="1h",
        last_bar_close_time=NOW + timedelta(minutes=1),
    )
    assert result.status == MarketHealthStatus.ERROR


def test_missing_bar_is_error() -> None:
    """没有任何已收盘K线时必须失败关闭。"""
    # 调用健康状态机，None不能被当作零延迟行情。
    result = evaluate_market_health(now=NOW, interval="30m", last_bar_close_time=None)
    assert result.status == MarketHealthStatus.ERROR
