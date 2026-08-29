"""订单意图与确定性客户端订单编号。"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """尚未发送到任何交易接口的不可变订单意图。"""

    strategy_run_id: str
    qualification_hash: str
    instrument_id: int
    symbol: str
    interval: str
    bar_time: datetime
    side: str
    position_side: str
    quantity: Decimal
    leverage: int
    reduce_only: bool = False
    order_type: str = "MARKET"
    price: Decimal | None = None
    stop_price: Decimal | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.bar_time.tzinfo is None:
            raise ValueError("bar_time must be timezone-aware")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if self.position_side not in {"LONG", "SHORT"}:
            raise ValueError("position_side must be LONG or SHORT")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if not 1 <= self.leverage <= 125:
            raise ValueError("leverage must be in [1, 125]")
        if not self.strategy_run_id or not self.qualification_hash:
            raise ValueError("strategy run and qualification hash are required")


def build_client_order_id(intent: OrderIntent) -> str:
    """根据策略、K线、品种和动作生成可重试的唯一订单编号。"""
    identity = "|".join(
        (
            intent.strategy_run_id,
            intent.qualification_hash,
            str(intent.instrument_id),
            intent.interval,
            intent.bar_time.isoformat(),
            intent.side,
            intent.position_side,
            "REDUCE" if intent.reduce_only else "ENTRY",
        )
    )
    # SHA-256截取40个十六进制字符，满足现有orders.client_order_id长度限制。
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]
    return f"gcq-{digest}"
