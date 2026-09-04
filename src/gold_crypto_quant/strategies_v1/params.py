"""震荡 v1.0 与顺势 v1.0 的全部可调参数。

参数集中在这里有两个原因：一是回测扫参要能整组替换；二是课程里凡是
"附近""明显""大量"的地方视频都没给数字（见 docs/课程规则提取.md 第七节），
那些空缺只能由外部配置来填，不该硬编码在策略逻辑里。

默认值的来源在每个字段的注释里标了课程条目号；没有条目号的是工程取值。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, TypeVar

CONFIG_PATH = Path("config/strategies-v1.json")

T = TypeVar("T", bound="_ParamBase")


@dataclass(frozen=True, slots=True)
class _ParamBase:
    """两个参数集共用的序列化与覆盖能力。"""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def replace(self: T, **changes: Any) -> T:
        """按字段名覆盖并重新走一次校验；扫参时逐组生成用这个。"""
        unknown = set(changes) - {f.name for f in fields(self)}
        if unknown:
            raise ValueError(f"未知参数：{sorted(unknown)}")
        return replace(self, **changes)

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{cls.__name__} 收到未知参数：{sorted(unknown)}")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class RangeParams(_ParamBase):
    """震荡策略 v1.0——按第八课的箱体定义交易。

    与 V5.8 最根本的差别：箱体上下沿是"≥2 次触碰连成的水平线"（L8R2/R3），
    不是布林带轨道。水平线固定不动，所以止盈目标不会像布林带那样一边涨一边跑。
    """

    # ---- 箱体识别 ----
    lookback_bars: int = 200
    """回看多少根 K 线找箱体。"""

    swing_window: int = 2
    """摆动高低点的左右确认根数：左右各 N 根都不更极端才算一个高/低点。"""

    touch_tolerance: float = 0.0015
    """两个高点算"同一价格附近"的相对容差。L8R2 明确说了没给数字。"""

    min_touches: int = 2
    """每条边最少触碰次数。L8R2/R3："连续两次以上"。"""

    min_box_height: float = 0.004
    """箱体最小高度（相对中价）。太窄的箱体手续费就吃光了。"""

    max_box_height: float = 0.060
    """箱体最大高度。太宽的不是箱体，是趋势里的回撤。"""

    use_body_for_edges: bool = True
    """画沿用实体还是影线。L8R2 要求剔除插针，实体更接近这个意思。"""

    # ---- 入场 ----
    entry_zone: float = 0.15
    """价格进入距沿多少（占箱高比例）算"到沿"。L8R6/R7 只说"触碰"。"""

    require_edge_rejection: bool = True
    """到沿后是否要求出现拒绝信号（收盘回到沿内）才进。L8R6/R7 的"冲高乏力"。"""

    # ---- 出场 ----
    exit_at_opposite: bool = True
    """到对侧沿止盈。L8R8："上沿空单到下沿及时止盈"。目标价是固定的水平线。"""

    middle_reduce_ratio: float = 0.0
    """到中位减仓比例。L8R8 说箱体中部不操作，默认 0；留着是为了和 V5.8 对照。"""

    stop_buffer: float = 0.25
    """止损放到沿外多少（占箱高比例）。L13R8 要求关键位下方留缓冲。"""

    # ---- 箱体失效 ----
    breakout_body_close: bool = True
    """实体收盘出沿才算突破，只有影线刺出不算。L8R10。"""

    breakout_volume_ratio: float = 1.5
    """突破需要的放量倍数。L8R11："1.5 至 2 倍"，取下限。"""

    exit_on_breakout: bool = True
    """实体收回沿外即无条件离场。L8R17。"""

    # ---- 风控 ----
    risk_per_trade: float = 0.0025
    """单笔风险占权益比例。L13R1 允许 1%–2%，这里沿用 V5.8 的 0.25%。"""

    max_positions: int = 1
    """同时最多持有几笔。"""

    def __post_init__(self) -> None:
        if self.lookback_bars < 20:
            raise ValueError("lookback_bars 至少 20 根才够找出两次触碰")
        if self.swing_window < 1:
            raise ValueError("swing_window 至少为 1")
        if not 0 < self.touch_tolerance < 0.1:
            raise ValueError("touch_tolerance 必须在 (0, 0.1)")
        if self.min_touches < 2:
            raise ValueError("L8R2/R3 要求每条边至少 2 次触碰")
        if not 0 < self.min_box_height < self.max_box_height:
            raise ValueError("箱体高度上下限设置无效")
        if not 0 < self.entry_zone <= 0.5:
            raise ValueError("entry_zone 必须在 (0, 0.5]，超过 0.5 就是箱体中部了")
        if not 0 <= self.middle_reduce_ratio < 1:
            raise ValueError("middle_reduce_ratio 必须在 [0, 1)")
        if self.stop_buffer <= 0:
            raise ValueError("stop_buffer 必须为正")
        if self.breakout_volume_ratio < 1:
            raise ValueError("breakout_volume_ratio 至少为 1")
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("risk_per_trade 必须在 (0, 0.02]，L13R1 的上限是 2%")
        if self.max_positions < 1:
            raise ValueError("max_positions 至少为 1")


@dataclass(frozen=True, slots=True)
class TrendParams(_ParamBase):
    """顺势策略 v1.0——按第一、七、十、十二课的顺势框架交易。

    与震荡策略的根本差别：这个只在有明确趋势时做，且方向由大周期决定，
    小周期只负责给入场点（L12R2/R4/R9）。
    """

    # ---- 多周期（L12）----
    direction_interval: str = "1h"
    """战略周期，只定方向不给买卖点。L12R2 用 4h，系统目前最大只有 1h。"""

    entry_interval: str = "15m"
    """执行周期，在方向约束下找精确入场。L12R4。"""

    require_direction_agreement: bool = True
    """是否强制小周期服从大周期。L12R9："只顺大趋势单向交易"。"""

    # ---- 趋势判定（L1）----
    min_waves: int = 3
    """至少几个浪形才构成趋势。L1R3。"""

    min_wave_bars: int = 2
    """同色 MACD 柱至少几根才算一浪。L1R1：只有一根时算前一段的延续。"""

    trend_lookback: int = 150
    """趋势判定的回看根数。"""

    # ---- 零轴（L10）----
    require_zero_axis: bool = True
    """是否要求 DIF 与交易方向在零轴同侧。L10R6/R11：零轴是强弱分水岭。"""

    # ---- 入场（L7R8/L7R12）----
    pullback_min: float = 0.20
    """回调至少要回撤上一浪的多少才算"回调到位"。"""

    pullback_max: float = 0.75
    """回调超过这个比例视为趋势可能已破坏，不再进。"""

    volume_shrink_ratio: float = 0.70
    """回调缩量阈值：回调段均量 ÷ 上涨段均量 要低于这个数。
    L7R8 说"最好缩到约前量的一半"，0.5 太严，默认放宽到 0.7。"""

    volume_expand_ratio: float = 1.20
    """转向根的放量倍数。L7R8 要求"阳量大于回调阴量"。"""

    require_volume: bool = True
    """是否启用量价条件。关掉可以单独衡量量价贡献了多少。"""

    # ---- 出场 ----
    stop_atr_multiple: float = 1.0
    """止损距离＝N × ATR，放在回调低点之外。L13R6/R8。"""

    atr_period: int = 14

    take_profit_r: float = 0.0
    """固定止盈倍数（R 的倍数），0 表示不设固定止盈、只靠跟踪止损。"""

    trail_start_r: float = 1.0
    """盈利达到几倍 R 之后开始跟踪止损。"""

    trail_atr_multiple: float = 2.0
    """跟踪止损与最高价的距离＝N × ATR。"""

    exit_on_trend_break: bool = True
    """趋势结构被破坏（浪形转向）即离场。L12R15。"""

    # ---- 风控 ----
    risk_per_trade: float = 0.0025
    max_positions: int = 1

    def __post_init__(self) -> None:
        valid = ("5m", "15m", "30m", "1h")
        if self.direction_interval not in valid or self.entry_interval not in valid:
            raise ValueError(f"周期必须取自 {valid}")
        order = {name: index for index, name in enumerate(valid)}
        if order[self.entry_interval] >= order[self.direction_interval]:
            raise ValueError("执行周期必须小于战略周期（L12R2/R4）")
        if self.min_waves < 2:
            raise ValueError("min_waves 至少为 2 才谈得上比较高低点")
        if self.min_wave_bars < 1:
            raise ValueError("min_wave_bars 至少为 1")
        if self.trend_lookback < 50:
            raise ValueError("trend_lookback 至少 50 根")
        if not 0 <= self.pullback_min < self.pullback_max <= 1:
            raise ValueError("回调区间必须满足 0 <= min < max <= 1")
        if not 0 < self.volume_shrink_ratio <= 1:
            raise ValueError("volume_shrink_ratio 必须在 (0, 1]")
        if self.volume_expand_ratio < 1:
            raise ValueError("volume_expand_ratio 至少为 1")
        if self.stop_atr_multiple <= 0 or self.atr_period < 2:
            raise ValueError("止损与 ATR 设置无效")
        if self.take_profit_r < 0 or self.trail_start_r < 0 or self.trail_atr_multiple <= 0:
            raise ValueError("止盈与跟踪止损设置无效")
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("risk_per_trade 必须在 (0, 0.02]")
        if self.max_positions < 1:
            raise ValueError("max_positions 至少为 1")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """两个策略的参数合起来，对应 config/strategies-v1.json 一个文件。"""

    range_params: RangeParams = RangeParams()
    trend_params: TrendParams = TrendParams()

    def to_dict(self) -> dict[str, Any]:
        return {"range": self.range_params.to_dict(), "trend": self.trend_params.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyConfig":
        return cls(
            range_params=RangeParams.from_dict(data.get("range", {})),
            trend_params=TrendParams.from_dict(data.get("trend", {})),
        )


def load_config(path: Path | None = None) -> StrategyConfig:
    """读配置文件；文件不存在时返回默认值，不报错。

    配置坏了要立刻知道——解析失败直接抛，不回落到默认值。悄悄用默认值跑，
    等于参数改了却没生效，比报错难查得多。
    """
    target = path or CONFIG_PATH
    if not target.exists():
        return StrategyConfig()
    return StrategyConfig.from_dict(json.loads(target.read_text(encoding="utf-8")))


def save_config(config: StrategyConfig, path: Path | None = None) -> Path:
    """把当前参数写回文件；扫参找到最优解之后落盘用这个。"""
    target = path or CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target
