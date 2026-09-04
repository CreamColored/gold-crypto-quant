"""为Apple风格监管页面聚合MySQL和影子状态文件中的只读数据。"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gold_crypto_quant.market_data.binance_history import BINANCE_LIVE_VENUE
from gold_crypto_quant.market_data.gate_history import GATE_LIVE_VENUE
from gold_crypto_quant.runtime.public_market_comparison_runner import (
    BINANCE_LIVE_STATE_PATH,
    GATE_LIVE_STATE_PATH,
)
from gold_crypto_quant.storage.market_bars import load_market_bars
from gold_crypto_quant.storage.models import (
    Instrument,
    MarketBar,
    MarketDataHealthState,
    MarketQuoteSecond,
    ShadowEquitySnapshot,
    ShadowTradeEvent,
    TradingAccount,
)
from gold_crypto_quant.storage.trading_switches import GLOBAL_SCOPE
from gold_crypto_quant.strategy.bollinger_range import (
    build_rotation_box_context,
    parameters_for_same_timeframe,
)

# 盘口面板固定展示这三个品种，与采集器和量化服务保持一致。
QUOTE_SYMBOLS = ("BTC_USDT", "ETH_USDT", "XAU_USDT")

VENUE_INFO = {
    GATE_LIVE_VENUE: {
        "label": "Gate",
        "short": "G",
        "state_path": GATE_LIVE_STATE_PATH,
        "color": "#0071e3",
    },
    BINANCE_LIVE_VENUE: {
        "label": "币安",
        "short": "B",
        "state_path": BINANCE_LIVE_STATE_PATH,
        "color": "#af52de",
    },
}


def _read_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _position_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for symbol, item in state.get("positions", {}).items():
        if not item.get("position_side"):
            continue
        rows.append(
            {
                "symbol": symbol,
                "side": item["position_side"],
                "interval": item.get("active_interval") or "-",
                "quantity": float(item.get("remaining_quantity") or 0),
                "entry_price": float(item.get("entry_price") or 0),
                "stop_price": float(item.get("stop_price") or 0),
                "middle_reduced": bool(item.get("middle_reduced")),
            }
        )
    return rows


def _active_boxes(state: dict[str, Any]) -> list[str]:
    boxes: list[str] = []
    for symbol, intervals in state.get("box_active", {}).items():
        for interval in ("5m", "15m", "30m", "1h"):
            if intervals.get(interval):
                boxes.append(f"{symbol.replace('_USDT', '')} {interval}")
    return boxes


def _latest_prices(session: Session, venue: str) -> dict[str, float]:
    prices: dict[str, float] = {}
    for symbol in ("BTC_USDT", "ETH_USDT", "XAU_USDT"):
        statement = (
            select(MarketBar.close_price)
            .join(Instrument, Instrument.id == MarketBar.instrument_id)
            .where(
                Instrument.venue == venue,
                Instrument.symbol == symbol,
                MarketBar.interval_code == "1m",
            )
            .order_by(MarketBar.open_time.desc())
            .limit(1)
        )
        value = session.scalar(statement)
        prices[symbol] = float(value) if value is not None else 0.0
    return prices


def _feed_health(session: Session, venue: str) -> dict[str, Any]:
    rows = session.execute(
        select(MarketDataHealthState.status, MarketDataHealthState.heartbeat_at)
        .join(Instrument, Instrument.id == MarketDataHealthState.instrument_id)
        .where(Instrument.venue == venue)
    ).all()
    if not rows:
        return {"status": "WAITING", "healthy": 0, "total": 0, "updated_at": None}
    healthy = sum(status == "HEALTHY" for status, _ in rows)
    latest = max(heartbeat for _, heartbeat in rows)
    return {
        "status": "HEALTHY" if healthy == len(rows) else "DEGRADED",
        "healthy": healthy,
        "total": len(rows),
        "updated_at": latest.replace(tzinfo=UTC).isoformat(),
    }


def is_super_admin(viewer: Any) -> bool:
    """只有明确标记为SUPER_ADMIN才算超管；角色缺失或异常一律按最小权限处理。"""
    return getattr(viewer, "role", "") == "SUPER_ADMIN"


def account_is_visible(account: Any, viewer: Any) -> bool:
    """判定单个账户对访问者是否可见，是纯逻辑，不触库因此可独立测试。

    超管看全部；普通用户只看归属自己的账户，系统影子账户对其一律不可见。
    """
    if is_super_admin(viewer):
        return True
    owner = getattr(account, "owner_user_id", None)
    viewer_id = getattr(viewer, "id", None)
    return (
        getattr(account, "owner_type", "") != "SYSTEM"
        and owner is not None
        and owner == viewer_id
    )


def visible_accounts(session: Session, viewer: Any) -> list[TradingAccount]:
    """返回该访问者可见的交易账户。

    所有涉及账户的查询都必须先经过这里取得范围，不允许绕开直接查全表。
    """
    statement = select(TradingAccount).order_by(TradingAccount.id)
    if not is_super_admin(viewer):
        statement = statement.where(
            TradingAccount.owner_user_id == getattr(viewer, "id", None),
            TradingAccount.owner_type != "SYSTEM",
        )
    return list(session.scalars(statement).all())


def build_overview(engine: Engine, *, viewer: Any) -> dict[str, Any]:
    """返回首页账户、权益曲线、仓位和最近事件；只包含访问者可见的账户。

    ``viewer`` 必须显式传入且没有默认值：漏传会直接报错，而不是静默返回全量数据。
    """
    accounts: list[dict[str, Any]] = []
    equity_series: dict[str, list[dict[str, Any]]] = {}
    with Session(engine) as session:
        account_models = [
            account
            for account in visible_accounts(session, viewer)
            if account.environment == "SHADOW"
        ]
        visible_ids = [account.id for account in account_models]
        for account in account_models:
            info = VENUE_INFO.get(account.venue)
            if info is None:
                continue
            state = _read_state(info["state_path"])
            equity = float(state.get("equity", 10_000.0))
            peak = float(state.get("peak_equity", equity))
            day_start = float(state.get("day_start_equity", equity))
            positions = _position_rows(state)
            closed_count = session.scalar(
                select(func.count(ShadowTradeEvent.id)).where(
                    ShadowTradeEvent.trading_account_id == account.id,
                    ShadowTradeEvent.event_type == "CLOSE",
                )
            ) or 0
            snapshots = session.execute(
                select(ShadowEquitySnapshot.snapshot_time, ShadowEquitySnapshot.equity)
                .where(ShadowEquitySnapshot.trading_account_id == account.id)
                .order_by(ShadowEquitySnapshot.snapshot_time.desc())
                .limit(720)
            ).all()
            snapshots.reverse()
            equity_series[info["label"]] = [
                {"time": time.replace(tzinfo=UTC).isoformat(), "value": float(value)}
                for time, value in snapshots
            ]
            accounts.append(
                {
                    "id": account.id,
                    "code": account.account_code,
                    "name": account.display_name,
                    "venue": account.venue,
                    "label": info["label"],
                    "color": info["color"],
                    "equity": equity,
                    "pnl": equity - 10_000.0,
                    "return_rate": equity / 10_000.0 - 1.0,
                    "daily_pnl": equity - day_start,
                    "drawdown": 0.0 if peak <= 0 else 1.0 - equity / peak,
                    "risk_state": (
                        "DRAWDOWN_HALTED"
                        if state.get("permanent_fuse")
                        else "DAILY_HALTED" if state.get("daily_blocked") else "NORMAL"
                    ),
                    "positions": positions,
                    "closed_trades": int(closed_count),
                    "active_boxes": _active_boxes(state),
                    "prices": _latest_prices(session, account.venue),
                    "health": _feed_health(session, account.venue),
                    "trading_enabled": account.trading_enabled,
                }
            )
        # 最近事件同样限定在可见账户内；无可见账户时直接返回空，不能落回全表。
        event_rows = (
            session.execute(
                select(ShadowTradeEvent, TradingAccount.display_name, TradingAccount.venue)
                .join(TradingAccount, TradingAccount.id == ShadowTradeEvent.trading_account_id)
                .where(ShadowTradeEvent.trading_account_id.in_(visible_ids))
                .order_by(ShadowTradeEvent.event_time.desc(), ShadowTradeEvent.id.desc())
                .limit(12)
            ).all()
            if visible_ids
            else []
        )
    recent_events = [
        {
            "id": event.id,
            "account": account_name,
            "venue": venue,
            "exchange": VENUE_INFO.get(venue, {}).get("label", venue),
            "time": event.event_time.replace(tzinfo=UTC).isoformat(),
            "symbol": event.symbol,
            "interval": event.interval_code,
            "title": event.title,
            "event_type": event.event_type,
            "severity": event.severity,
            "details": event.details,
        }
        for event, account_name, venue in event_rows
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "accounts": accounts,
        "equity_series": equity_series,
        "recent_events": recent_events,
        "live_trading": False,
        "order_submission": False,
    }


def build_switch_view(switches: dict[str, bool]) -> dict:
    """把三级开关整理成"总开关 + 每个交易所一组"的结构。

    上层关闭时下层由页面收起，但**下层状态原样保留**——重新打开上层会回到之前
    配置的状态。级联写入（关上层顺带把下层也改掉）看似直观，实际会把用户单独
    禁用某个品种的决定静默抹掉，而且抹掉这件事在页面上完全看不出来。
    """
    global_on = switches.get(GLOBAL_SCOPE, True)
    venues = []
    for venue, label in ((GATE_LIVE_VENUE, "Gate"), (BINANCE_LIVE_VENUE, "币安")):
        venue_on = switches.get(venue, True)
        symbols = []
        for symbol in QUOTE_SYMBOLS:
            key = f"{venue}:{symbol}"
            own = switches.get(key, True)
            symbols.append({
                "scope_key": key,
                "label": symbol.replace("_USDT", ""),
                "sub": symbol,
                "enabled": own,
                "effective": global_on and venue_on and own,
            })
        venues.append({
            "scope_key": venue,
            "label": label,
            "enabled": venue_on,
            "effective": global_on and venue_on,
            # 上层关闭时前端收起品种列表；数据仍然返回，展开即可看到原状态。
            "collapsed": not (global_on and venue_on),
            "symbols": symbols,
        })
    known = [GLOBAL_SCOPE]
    for venue in venues:
        known.append(venue["scope_key"])
        known.extend(item["scope_key"] for item in venue["symbols"])
    allowed = [
        item for venue in venues for item in venue["symbols"] if item["effective"]
    ]
    total = sum(len(venue["symbols"]) for venue in venues)
    return {
        "global": {"scope_key": GLOBAL_SCOPE, "enabled": global_on, "effective": global_on},
        "venues": venues,
        "known_keys": known,
        "allowed": len(allowed),
        "total": total,
    }


def build_live_quotes(
    engine: Engine,
    *,
    venue: str,
    symbol: str,
    stale_after_seconds: int = 10,
) -> dict:
    """返回指定交易所与品种的最新盘口。

    数据来自盘口采集器写入的秒级聚合表，不是浏览器直连交易所——展示的是
    "最近一秒的极值"，不是逐帧跳动。因此必须带上数据年龄：采集器停掉时页面要
    看得出来，而不是继续显示几小时前的价格还一副正常样子。

    买一取该秒的最高买价、卖一取该秒的最低卖价，即这一秒里最紧的盘口。
    """
    now = datetime.now(UTC)
    with Session(engine) as session:
        record = session.execute(
            select(MarketQuoteSecond)
            .join(Instrument, Instrument.id == MarketQuoteSecond.instrument_id)
            .where(Instrument.venue == venue, Instrument.symbol == symbol)
            .order_by(MarketQuoteSecond.bucket_time.desc())
            .limit(1)
        ).scalar_one_or_none()

    if record is None:
        return {
            "venue": venue, "symbol": symbol, "available": False,
            "reason": "盘口采集器尚未写入该品种数据",
            "generated_at": now.isoformat(),
        }
    age = (now - record.bucket_time.replace(tzinfo=UTC)).total_seconds()
    # 展示当前价必须用同一帧的快照：极值来自秒内不同瞬间，
    # 拿最高买价配最低卖价会得到买一高于卖一的交叉盘口。
    # 快照列是后加的，改动前写入的旧行没有值，退回用极值中点近似。
    if record.bid_close is not None and record.ask_close is not None:
        bid, ask = float(record.bid_close), float(record.ask_close)
    else:
        bid = (float(record.bid_low) + float(record.bid_high)) / 2
        ask = (float(record.ask_low) + float(record.ask_high)) / 2
    return {
        "venue": venue,
        "symbol": symbol,
        "available": True,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "spread": ask - bid,
        "second_low": float(record.bid_low),
        "second_high": float(record.ask_high),
        "frame_count": record.frame_count,
        "bucket_time": record.bucket_time.replace(tzinfo=UTC).isoformat(),
        "age_seconds": round(age, 1),
        "stale": age > stale_after_seconds,
        "generated_at": now.isoformat(),
    }


def build_market_chart(
    engine: Engine,
    *,
    venue: str,
    symbol: str,
    interval: str,
    limit: int = 160,
) -> dict[str, Any]:
    """返回交互K线、布林带和震荡判断所需数据。"""
    if venue not in VENUE_INFO:
        raise ValueError("unsupported venue")
    if symbol not in {"BTC_USDT", "ETH_USDT", "XAU_USDT"}:
        raise ValueError("unsupported symbol")
    if interval not in {"1m", "5m", "15m", "30m", "1h"}:
        raise ValueError("unsupported interval")
    bars = load_market_bars(symbol, interval, engine=engine, venue=venue, limit=limit)
    display = bars.copy()
    if interval != "1m":
        context = build_rotation_box_context(
            bars,
            parameters_for_same_timeframe(interval),
        )
        display = display.join(
            context[["bb_upper", "bb_middle", "bb_lower", "box_candidate"]]
        ).rename(columns={"box_candidate": "box_active"})
    else:
        middle = display["close"].rolling(20).mean()
        deviation = display["close"].rolling(20).std(ddof=0)
        display["bb_middle"] = middle
        display["bb_upper"] = middle + deviation * 2
        display["bb_lower"] = middle - deviation * 2
        display["box_active"] = False
    display = display.dropna(subset=["bb_upper", "bb_middle", "bb_lower"])
    rows = []
    for timestamp, row in display.iterrows():
        rows.append(
            {
                "time": timestamp.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "upper": float(row["bb_upper"]),
                "middle": float(row["bb_middle"]),
                "lower": float(row["bb_lower"]),
                "box_active": bool(row["box_active"]),
            }
        )
    latest = rows[-1] if rows else None
    return {
        "venue": venue,
        "exchange": VENUE_INFO[venue]["label"],
        "symbol": symbol,
        "interval": interval,
        "rows": rows,
        "latest": latest,
    }


def build_trade_events(
    engine: Engine,
    *,
    viewer: Any,
    venue: str | None = None,
    symbol: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """按交易所和品种分页返回影子交易事件；只包含访问者可见账户的事件。

    ``viewer`` 无默认值，漏传会直接报错而不是泄露他人交易明细。
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    with Session(engine) as session:
        visible_ids = [account.id for account in visible_accounts(session, viewer)]
        if not visible_ids:
            return {
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "total_pages": 1,
            }
        base_statement = (
            select(ShadowTradeEvent)
            .join(TradingAccount, TradingAccount.id == ShadowTradeEvent.trading_account_id)
            .where(ShadowTradeEvent.trading_account_id.in_(visible_ids))
        )
        if venue:
            base_statement = base_statement.where(TradingAccount.venue == venue)
        if symbol:
            base_statement = base_statement.where(ShadowTradeEvent.symbol == symbol)

        total = session.execute(
            select(func.count()).select_from(base_statement.subquery())
        ).scalar_one()

        statement = (
            select(ShadowTradeEvent, TradingAccount.display_name, TradingAccount.venue)
            .join(TradingAccount, TradingAccount.id == ShadowTradeEvent.trading_account_id)
            .where(ShadowTradeEvent.trading_account_id.in_(visible_ids))
            .order_by(ShadowTradeEvent.event_time.desc(), ShadowTradeEvent.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        if venue:
            statement = statement.where(TradingAccount.venue == venue)
        if symbol:
            statement = statement.where(ShadowTradeEvent.symbol == symbol)
        rows = session.execute(statement).all()

    items = [
        {
            "id": event.id,
            "account": account_name,
            "exchange": VENUE_INFO.get(event_venue, {}).get("label", event_venue),
            "venue": event_venue,
            "time": event.event_time.replace(tzinfo=UTC).isoformat(),
            "symbol": event.symbol,
            "interval": event.interval_code,
            "title": event.title,
            "event_type": event.event_type,
            "severity": event.severity,
            "details": event.details,
        }
        for event, account_name, event_venue in rows
    ]
    total_pages = max((total + page_size - 1) // page_size, 1)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


# 盘口最新数据超过这个秒数即判定采集异常。采集器每秒落盘一次，
# 30秒已经是60轮没写进来了，不可能是正常抖动。
QUOTE_COLLECTOR_STALE_SECONDS = 30
QUOTE_COLLECTOR_DOWN_SECONDS = 300


def build_quote_collector_health(engine: Engine) -> dict[str, Any]:
    """汇总盘口采集器各条流的新鲜度。

    行情页的实时价是浏览器直连交易所的，采集器停了那里照样跳动、完全看不出来；
    而策略与回测用的正是采集器落库的这份数据，因此它的健康必须单独有观察点。
    """
    now = datetime.now(UTC)
    streams: list[dict[str, Any]] = []
    with Session(engine) as session:
        for venue, label in ((GATE_LIVE_VENUE, "Gate"), (BINANCE_LIVE_VENUE, "币安")):
            for symbol in QUOTE_SYMBOLS:
                record = session.execute(
                    select(MarketQuoteSecond)
                    .join(Instrument, Instrument.id == MarketQuoteSecond.instrument_id)
                    .where(Instrument.venue == venue, Instrument.symbol == symbol)
                    .order_by(MarketQuoteSecond.bucket_time.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if record is None:
                    streams.append({"label": label, "symbol": symbol, "age_seconds": None,
                                    "frame_count": 0, "status": "NO_DATA"})
                    continue
                age = (now - record.bucket_time.replace(tzinfo=UTC)).total_seconds()
                status = (
                    "HEALTHY" if age <= QUOTE_COLLECTOR_STALE_SECONDS
                    else "STALE" if age <= QUOTE_COLLECTOR_DOWN_SECONDS
                    else "STOPPED"
                )
                streams.append({
                    "label": label, "symbol": symbol, "age_seconds": round(age, 1),
                    "frame_count": record.frame_count, "status": status,
                })
    ages = [item["age_seconds"] for item in streams if item["age_seconds"] is not None]
    # 整体取最差的一条：任何一条流停了都说明采集不完整，不能被其他流掩盖。
    if not ages:
        overall = "NO_DATA"
    elif max(ages) > QUOTE_COLLECTOR_DOWN_SECONDS:
        overall = "STOPPED"
    elif max(ages) > QUOTE_COLLECTOR_STALE_SECONDS:
        overall = "STALE"
    else:
        overall = "HEALTHY"
    return {
        "status": overall,
        "worst_age_seconds": max(ages) if ages else None,
        "healthy": sum(1 for item in streams if item["status"] == "HEALTHY"),
        "total": len(streams),
        "streams": streams,
    }


def build_system_status(engine: Engine | None = None) -> dict[str, Any]:
    """返回Mac和本地进程的只读运行状态；传入engine时一并给出采集器健康。"""
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path.cwd()))
    boot = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
    lock_path = Path(".runtime/public-market-comparison.lock")
    comparison_pid: int | None = None
    try:
        comparison_pid = int(lock_path.read_text().strip())
        if not psutil.pid_exists(comparison_pid):
            comparison_pid = None
    except (OSError, ValueError):
        comparison_pid = None
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "hostname": os.uname().nodename,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory_percent": memory.percent,
        "memory_used_gib": (memory.total - memory.available) / 1024**3,
        "memory_total_gib": memory.total / 1024**3,
        "disk_percent": disk.percent,
        "disk_used_gib": disk.used / 1024**3,
        "disk_total_gib": disk.total / 1024**3,
        "uptime_seconds": (datetime.now(UTC) - boot).total_seconds(),
        "comparison_pid": comparison_pid,
        "comparison_running": comparison_pid is not None,
        "web_pid": os.getpid(),
        "live_trading": False,
        "order_submission": False,
        "quote_collector": build_quote_collector_health(engine) if engine is not None else None,
    }
