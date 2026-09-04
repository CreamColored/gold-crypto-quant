"""Redis K线视图的结构测试。

三条不变量：同一 score 必须是替换而不是并存（ZSET 成员是字符串，塞两个不同 JSON
会得到两条）；序列必须自限长（不靠消费者活着来控制内存）；在途标记必须可区分，
否则策略分不清"这根还在走"和"这根已经定了"。
"""

from datetime import UTC, datetime, timedelta

from gold_crypto_quant.storage.redis_bars import (
    TOTAL_BAR_LIMIT,
    RedisBar,
    publish_bars,
    read_bars,
)

BASE = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)


class _FakeRedis:
    """够用的 ZSET 替身：成员唯一、按 score 排序，语义与真实 Redis 一致。"""

    def __init__(self) -> None:
        self.sets: dict[str, dict[str, int]] = {}

    def register_script(self, _source):
        def run(keys, args):
            key, limit = keys[0], int(keys[1])
            members = self.sets.setdefault(key, {})
            for i in range(0, len(args), 2):
                score, member = int(args[i]), args[i + 1]
                for existing, existing_score in list(members.items()):
                    if existing_score == score:
                        del members[existing]
                members[member] = score
            if len(members) > limit:
                ordered = sorted(members.items(), key=lambda kv: kv[1])
                for member, _ in ordered[: len(members) - limit]:
                    del members[member]
            return len(members)

        return run

    def zrange(self, key, start, end):  # noqa: ARG002
        return [m for m, _ in sorted(self.sets.get(key, {}).items(), key=lambda kv: kv[1])]


def _bar(minutes: int, close: float, *, provisional: bool = False) -> RedisBar:
    moment = BASE + timedelta(minutes=minutes)
    return RedisBar(moment, 1.0, 2.0, 0.5, close, 10.0, 15.0, provisional=provisional)


def test_same_timestamp_replaces_instead_of_coexisting() -> None:
    """官方K线到达时必须顶掉同一时刻的在途K线，不能并存成两根。"""
    client = _FakeRedis()
    publish_bars("V", "S", "1m", [_bar(0, 1.5), _bar(1, 1.6, provisional=True)], client=client)
    publish_bars("V", "S", "1m", [_bar(1, 9.9)], client=client)
    frame = read_bars("V", "S", "1m", client=client)
    assert len(frame) == 2
    assert frame["close"].iloc[-1] == 9.9
    assert not bool(frame["provisional"].iloc[-1])


def test_sequence_is_self_limiting() -> None:
    """写多少根都裁到上限——内存不靠消费者活着来控制。"""
    client = _FakeRedis()
    publish_bars("V", "S", "1m", [_bar(i, float(i)) for i in range(900)], client=client)
    frame = read_bars("V", "S", "1m", client=client)
    assert len(frame) == TOTAL_BAR_LIMIT
    # 裁掉的是最旧的，最新那根必须留下
    assert frame["close"].iloc[-1] == 899.0


def test_provisional_can_be_excluded() -> None:
    """策略要能只取已收线的部分，否则分不清哪根还在走。"""
    client = _FakeRedis()
    publish_bars("V", "S", "1m", [_bar(0, 1.0), _bar(1, 2.0, provisional=True)], client=client)
    assert len(read_bars("V", "S", "1m", client=client)) == 2
    closed = read_bars("V", "S", "1m", client=client, include_provisional=False)
    assert len(closed) == 1 and closed["close"].iloc[-1] == 1.0


def test_frame_shape_matches_mysql_path() -> None:
    """列名与索引要和 load_market_bars 一致，降级读 MySQL 时才不用另写一套解析。"""
    client = _FakeRedis()
    publish_bars("V", "S", "1m", [_bar(0, 1.0)], client=client)
    frame = read_bars("V", "S", "1m", client=client)
    assert list(frame.columns)[:6] == ["open", "high", "low", "close", "volume", "quote_volume"]
    assert frame.index.name == "open_time" and frame.index.tz is not None


def test_only_the_tail_is_fetched() -> None:
    """取全量再在Python里切会白解析几百条JSON。

    读在途K线传的是 limit=2，却要先解析501条；每秒六次就是每秒三千条无用解析，
    Redis 出站流量几乎全是这个。
    """
    client = _FakeRanges()
    publish_bars("V", "S", "1m", [_bar(i, float(i)) for i in range(300)], client=client)
    client.calls.clear()
    read_bars("V", "S", "1m", client=client, limit=2, include_provisional=True)
    assert client.calls == [(-2, -1)]


def test_excluding_provisional_fetches_one_extra() -> None:
    """序列里最多一根在途K线且必然在末尾，多取一根就够排除它。"""
    client = _FakeRanges()
    publish_bars("V", "S", "1m", [_bar(i, float(i)) for i in range(300)], client=client)
    client.calls.clear()
    read_bars("V", "S", "1m", client=client, limit=5, include_provisional=False)
    assert client.calls == [(-6, -1)]


def test_tail_fetch_still_returns_the_right_bars() -> None:
    """只取尾部之后，返回的内容必须和取全量再切完全一致。"""
    client = _FakeRanges()
    bars = [_bar(i, float(i)) for i in range(50)]
    bars.append(_bar(50, 99.0, provisional=True))
    publish_bars("V", "S", "1m", bars, client=client)
    frame = read_bars("V", "S", "1m", client=client, limit=3, include_provisional=True)
    assert list(frame["close"]) == [48.0, 49.0, 99.0]
    closed = read_bars("V", "S", "1m", client=client, limit=3, include_provisional=False)
    assert list(closed["close"]) == [47.0, 48.0, 49.0]


class _FakeRanges(_FakeRedis):
    """记录 zrange 的实际取值范围，用来证明没有取全量。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[int, int]] = []

    def zrange(self, key, start, end):
        self.calls.append((start, end))
        ordered = [m for m, _ in sorted(self.sets.get(key, {}).items(), key=lambda kv: kv[1])]
        return ordered[start:] if end == -1 else ordered[start:end + 1]
