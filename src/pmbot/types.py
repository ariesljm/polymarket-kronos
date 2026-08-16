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


@dataclass
class Position:
    """持仓：方向、入场价、股数、入场时窗口剩余秒、所属窗口。"""

    direction: Direction
    entry_price: float
    size: float
    entered_remaining_sec: int
    window_start: int
    entry_balance: float | None = None  # 入场时钱包余额快照（实盘盈亏基准，查询失败为 None）


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

def token_for(market, direction: Direction) -> str:
    """方向 → 市场 token（UP→yes，DOWN→no）。单一事实源（main_loop/lifecycle 共用）。"""
    return market.yes_token_id if direction is Direction.UP else market.no_token_id
