"""策略决策引擎。

纯函数 decide(config, state, market, signal) → Action：
所有策略参数从配置注入，不写死数字；外部（数据/执行/时钟）均为注入状态。
"""

from __future__ import annotations

from pmbot.config import Config
from pmbot.exit_rules import position_exit_levels
from pmbot.types import Action, ActionType, Direction, MarketView, Position, Signal, StateView


def decide(config: Config, state: StateView, market: MarketView, signal: Signal) -> Action:
    """根据当前状态与信号决定下一个动作。

    state: 连续亏损、当日亏损、本窗口是否已下注、是否暂停。
    market: 距窗口结束秒数、目标方向 best ask/bid、当前持仓、挂单。
    """
    # 熔断优先于一切交易动作；人工暂停时不产生任何交易动作
    if state.paused:
        return Action(ActionType.SKIP)
    if state.consecutive_losses >= config.max_consecutive_losses:
        return Action(ActionType.PAUSE, reason="consecutive_losses")
    if state.daily_loss >= config.max_daily_loss:
        return Action(ActionType.PAUSE, reason="daily_loss")

    position = market.position
    if position is not None:
        return _manage_position(config, position, market.best_bid, market.remaining_sec)

    pending = market.pending_order
    if pending is not None:
        # 挂单未成交，接近窗口结束则撤单
        if market.remaining_sec <= config.cancel_before_end_sec:
            return Action(ActionType.CANCEL)
        return Action(ActionType.SKIP)

    if state.window_bet_placed:
        # 每窗口每标的最多一注
        return Action(ActionType.SKIP)

    return _maybe_enter(config, signal, market.best_ask, market.remaining_sec)


def _manage_position(config: Config, position: Position, best_bid: float | None, remaining_sec: int) -> Action:
    if best_bid is None:
        return Action(ActionType.SKIP)
    # 百分比止盈止损（相对入场价）：共享 exit_rules 单一事实源（回测/面板同公式）
    tp, sl = position_exit_levels(
        position.entry_price, config.take_profit, config.stop_loss,
        tp_max=config.take_profit_max,
    )
    if best_bid >= tp:
        return Action(ActionType.SELL, reason="take_profit")
    # 窗口末两条独立规则:
    # 1) 亏损：窗口结束前 exit_loss_before_end_sec 内浮亏 → 市价离场
    if remaining_sec <= config.exit_loss_before_end_sec and best_bid <= position.entry_price:
        return Action(ActionType.SELL, reason="window_end")
    # 2) 盈利：窗口结束前 hold_until_end_sec 内浮盈 → 持有到结算
    if remaining_sec <= config.hold_until_end_sec and best_bid > position.entry_price:
        return Action(ActionType.SKIP)
    if best_bid <= sl:
        return Action(ActionType.SELL, reason="stop_loss")
    elapsed = position.entered_remaining_sec - remaining_sec
    if elapsed >= config.time_stop_min * 60 and best_bid <= position.entry_price:
        return Action(ActionType.SELL, reason="time_stop")
    return Action(ActionType.SKIP)


def _maybe_enter(config: Config, signal: Signal, best_ask: float | None, remaining_sec: int) -> Action:
    if signal.direction is Direction.SKIP:
        return Action(ActionType.SKIP)
    # 窗口结束前 N 秒禁止买入（中途启动时避免窗口末仓）
    if remaining_sec <= config.no_entry_before_end_sec:
        return Action(ActionType.SKIP)
    # 市价入场：预测后立即按 1 USDC 目标买入（份额=金额/盘口价，可小数，无 5 股限制）
    if signal.direction is Direction.UP and signal.p_up > config.p_up_buy:
        return Action(ActionType.PLACE_MARKET, direction=Direction.UP, amount=config.amount_per_trade)
    if signal.direction is Direction.DOWN and signal.p_up < config.p_down_buy:
        return Action(ActionType.PLACE_MARKET, direction=Direction.DOWN, amount=config.amount_per_trade)
    return Action(ActionType.SKIP)
