"""宏观数据发布前后的开仓静默窗口。

2026-09-04 的非农是这套东西的由来：20:30:00 数据发布，BTC 一分钟内从 81285 砸到
80130（振幅1155点、成交量放大12倍），影子账户在 20:30:3x 因为"跌破下轨且连续3秒
还在轨道外"判定为有效触碰，开了两笔多单，同一分钟内双双止损。

**停留确认在这类行情里起反作用**：它的前提是"持续触碰比瞬时插针可信"，而在数据
驱动的单边启动里，持续待在轨道外恰恰说明压力还在，不是要回归。

箱体判定也拦不住：量比看的是已收线K线，12:29 那根量只有78万一切正常，爆量发生在
12:30 这根之内；而在途K线没有成交量（盘口推送里不含），当根爆量在收线前不可见。

所以不从形态上识别，改从**时间**上规避——非农、CPI、FOMC 的发布时刻是已知的，
时间是确定的，形态是猜的。

**只挡开仓。** 已有仓位的止损、减仓、止盈在静默期内照常执行——静默是为了少开单，
不是为了让风险动作停摆。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 美国经济数据按美东时间发布，而美东有夏令时：8:30 ET 在夏令时是 12:30 UTC、
# 冬令时是 13:30 UTC。写死 UTC 偏移会让窗口半年错一个小时，必须用时区库换算。
EASTERN = ZoneInfo("America/New_York")

DEFAULT_CONFIG_PATH = Path("config/macro-events.json")
# 发布前后各静默多久。发布前也要挡：数据出来前几分钟盘口就开始变薄，
# 那时候的触轨同样不是震荡回归。
DEFAULT_BEFORE_MINUTES = 15
DEFAULT_AFTER_MINUTES = 15


@dataclass(frozen=True, slots=True)
class MacroEvent:
    """一次宏观数据发布。"""

    name: str
    moment: datetime

    def window(self, before: int, after: int) -> tuple[datetime, datetime]:
        return (
            self.moment - timedelta(minutes=before),
            self.moment + timedelta(minutes=after),
        )


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> datetime:
    """当月第 nth 个星期 weekday（周一=0）的日期。"""
    first = datetime(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _recurring_moments(rule: str, at: time, around: datetime) -> list[datetime]:
    """展开重复规则；只算 around 所在月及前后一个月，足够覆盖任何窗口。"""
    moments: list[datetime] = []
    for delta in (-1, 0, 1):
        month = around.month + delta
        year = around.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        if rule == "first_friday":
            day = _nth_weekday(year, month, 4, 1)
        elif rule == "second_wednesday":
            day = _nth_weekday(year, month, 2, 2)
        else:
            continue
        local = datetime.combine(day.date(), at, tzinfo=EASTERN)
        moments.append(local.astimezone(UTC))
    return moments


def load_events(
    around: datetime, *, config_path: Path | None = None
) -> list[MacroEvent]:
    """读取事件表并展开成 around 附近的具体时刻。

    配置缺失或损坏时退回只有非农的默认表——**宁可少挡也不能因为配置问题静默失效**，
    但非农是最确定、影响最大的一个，硬编码兜底。
    """
    path = config_path or DEFAULT_CONFIG_PATH
    payload: dict = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    recurring = payload.get("recurring") or [
        {"name": "美国非农就业", "rule": "first_friday", "time_et": "08:30"}
    ]
    events: list[MacroEvent] = []
    for item in recurring:
        try:
            hour, minute = (int(part) for part in str(item["time_et"]).split(":"))
            for moment in _recurring_moments(str(item["rule"]), time(hour, minute), around):
                events.append(MacroEvent(str(item["name"]), moment))
        except (KeyError, ValueError):
            continue
    for item in payload.get("dates") or []:
        try:
            zone = ZoneInfo(str(item.get("tz", "America/New_York")))
            local = datetime.fromisoformat(str(item["at"])).replace(tzinfo=zone)
            events.append(MacroEvent(str(item["name"]), local.astimezone(UTC)))
        except (KeyError, ValueError, OSError):
            continue
    return events


def active_blackout(
    now: datetime,
    *,
    before: int = DEFAULT_BEFORE_MINUTES,
    after: int = DEFAULT_AFTER_MINUTES,
    events: Iterable[MacroEvent] | None = None,
    config_path: Path | None = None,
) -> MacroEvent | None:
    """当前时刻是否落在某次发布的静默窗口内；是则返回那次事件。"""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    candidates = events if events is not None else load_events(now, config_path=config_path)
    for event in candidates:
        start, end = event.window(before, after)
        if start <= now <= end:
            return event
    return None
