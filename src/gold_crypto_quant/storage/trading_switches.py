"""交易开关的读写与层级判定。

关闭一个开关只会阻止开仓，**永远不会促成任何交易**——已有仓位的止损、中轨减仓、
对侧轨止盈与阶梯延续照常执行。语义是"只出不进"，不是冻结持仓。
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import Engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from gold_crypto_quant.storage.database import build_engine
from gold_crypto_quant.storage.models import TradingSwitch

GLOBAL_SCOPE = "GLOBAL"


def symbol_scope(venue: str, symbol: str) -> str:
    """品种级开关的范围键。"""
    return f"{venue}:{symbol}"


def resolve_entry_allowed(switches: dict[str, bool], venue: str, symbol: str) -> bool:
    """总开关 → 交易所开关 → 品种开关，逐级取"与"。

    缺失的键按"开启"处理：新接入的交易所或品种不应因为还没建记录就被静默禁用，
    那样故障会以"策略莫名不开单"的形式出现，很难排查。要禁用必须显式写一条关闭记录。

    这是不碰数据库的纯函数，便于独立测试。
    """
    if not switches.get(GLOBAL_SCOPE, True):
        return False
    if not switches.get(venue, True):
        return False
    return switches.get(symbol_scope(venue, symbol), True)


def load_switches(engine: Engine | None = None) -> dict[str, bool]:
    """读取全部开关；返回 范围键 → 是否允许开仓。"""
    engine = engine or build_engine()
    with Session(engine) as session:
        rows = session.execute(select(TradingSwitch.scope_key, TradingSwitch.enabled)).all()
    return {key: bool(enabled) for key, enabled in rows}


def set_switch(
    scope_key: str,
    enabled: bool,
    *,
    updated_by: str,
    engine: Engine | None = None,
) -> None:
    """写入一个开关；同一范围键幂等更新，不产生重复行。"""
    engine = engine or build_engine()
    statement = mysql_insert(TradingSwitch).values(
        scope_key=scope_key,
        enabled=enabled,
        updated_by=updated_by,
        updated_at=datetime.now(UTC),
    )
    with Session(engine) as session:
        session.execute(
            statement.on_duplicate_key_update(
                enabled=statement.inserted.enabled,
                updated_by=statement.inserted.updated_by,
                updated_at=statement.inserted.updated_at,
            )
        )
        session.commit()


def ensure_default_switches(
    venues: Iterable[str],
    symbols: Iterable[str],
    *,
    engine: Engine | None = None,
) -> None:
    """为总开关、各交易所与各品种建立默认开启的记录，已存在的不覆盖。"""
    engine = engine or build_engine()
    keys = [GLOBAL_SCOPE]
    for venue in venues:
        keys.append(venue)
        keys.extend(symbol_scope(venue, symbol) for symbol in symbols)
    with Session(engine) as session:
        existing = {
            key
            for (key,) in session.execute(select(TradingSwitch.scope_key)).all()
        }
        missing = [key for key in keys if key not in existing]
        if not missing:
            return
        session.execute(
            mysql_insert(TradingSwitch).values(
                [
                    {"scope_key": key, "enabled": True, "updated_by": "system",
                     "updated_at": datetime.now(UTC)}
                    for key in missing
                ]
            ).prefix_with("IGNORE")
        )
        session.commit()
