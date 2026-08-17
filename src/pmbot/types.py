"""领域类型：方向、信号、动作、持仓、挂单、决策视图。

StateView / MarketView 是决策引擎（engine.decide）的输入契约——
主循环构造、引擎消费、监控面板展示共用同一类型，避免裸 dict 魔法键漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypedDict


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    SKIP = "skip"


class SignalContext(TypedDict, total=False):
    """Strategy.generate_signal 的上下文入参（键均为可选，策略自行兜底）。"""

    now_ms: int


@dataclass(frozen=True)
class Signal:
    """策略输出的信号。p_up 为预测上涨概率（[0,1]）。"""

    direction: Direction
    p_up: float

    def __post_init__(self):
        # 兼容字符串形式（如 "up"），统一为枚举，避免裸串穿透类型判断
        if isinstance(self.direction, str):
            object.__setattr__(self, "direction", Direction(self.direction))


class ActionType(str, Enum):
    PLACE_MARKET = "place_market"
    CANCEL = "cancel"
    SELL = "sell"
    SKIP = "skip"
    PAUSE = "pause"


@dataclass(frozen=True)
class Action:
    type: ActionType
    direction: Direction | None = None
    price: float | None = None
    amount: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Fill:
    """市价成交结果（跨 seam 类型化契约，替代成交 dict 魔法键）。

    order_id：真实订单号（卖出兑付聚合用）；None = 无真实订单。
    avg_price：成交均价（买/卖通用）；执行器取不到时已自行回退（卖：best_bid）。
    filled_size：实际成交股数（买入语义；卖出为 None）。
    """

    order_id: str | None
    avg_price: float | None
    filled_size: float | None = None


# ---- Polymarket 市场格式解析（slug/outcome）与持仓重建：单一事实源 ----
# 市场格式（slug 结构、outcome 标签）曾被当字符串手工处理散落四处
# （wallet 幽灵判定/接管、trade_history 流水配对、引擎挂单成交）。


def window_start_from_slug(slug: str) -> int | None:
    """slug → 窗口起点秒（如 eth-updown-5m-1786897500 → 1786897500）；无法解析返回 None。"""
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (ValueError, AttributeError):
        return None


def symbol_from_slug(slug: str) -> str:
    """slug → 标的符号（大写，如 eth-updown-5m-… → ETH）。"""
    return str(slug).split("-", 1)[0].upper() if slug else ""


def direction_from_outcome(outcome: str) -> Direction | None:
    """outcome → 方向（"Up"→UP，"Down"→DOWN）；无法识别返回 None。"""
    o = str(outcome or "").lower()
    if o == "up":
        return Direction.UP
    if o == "down":
        return Direction.DOWN
    return None


def rebuilt_position(direction: Direction, entry_price: float, size: float,
                     window_start: int, now_sec: int, step_sec: int) -> Position:
    """从外部数据重建持仓（挂单成交/API 成交/钱包接管三条路径共用）。

    entered_remaining_sec = 窗口剩余秒（window_start + step − now，负数截 0）——
    时间止损/收益持有时钟从重建时刻重新计时。
    """
    return Position(
        direction=direction,
        entry_price=entry_price,
        size=size,
        entered_remaining_sec=max(0, window_start + step_sec - now_sec),
        window_start=window_start,
    )


@dataclass
class Position:
    """持仓：方向、入场价、股数、入场时窗口剩余秒、所属窗口。"""

    direction: Direction
    entry_price: float
    size: float
    entered_remaining_sec: int
    window_start: int


@dataclass
class PendingOrder:
    """未成交限价单。"""

    direction: Direction
    price: float
    size: float
    order_id: str
    created_sec: int


@dataclass(frozen=True)
class StateView:
    """决策引擎输入：交易状态视图（熔断/暂停/本窗口标记）。"""

    consecutive_losses: int
    daily_loss: float
    window_bet_placed: bool
    paused: bool


@dataclass(frozen=True)
class MarketView:
    """决策引擎输入：当前窗口市场视图。"""

    remaining_sec: int
    best_ask: float | None
    best_bid: float | None
    position: Position | None
    pending_order: PendingOrder | None
    elapsed_sec: int = 0  # 窗口已进行秒数（开仓延迟判断用，0 = 未知）

def token_for(market, direction: Direction) -> str:
    """方向 → 市场 token（UP→yes，DOWN→no）。单一事实源（main_loop/lifecycle 共用）。"""
    return market.yes_token_id if direction is Direction.UP else market.no_token_id
