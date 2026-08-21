"""策略决策引擎。

纯函数 decide(config, state, market, signal) → Action：
所有策略参数从配置注入，不写死数字；外部（数据/执行/时钟）均为注入状态。
"""

from __future__ import annotations

from pmbot.config import EngineConfig
from pmbot.exit_rules import position_exit_levels
from pmbot.types import Action, ActionType, Direction, MarketView, Position, Signal, StateView


# 熔断原因 key（Action.reason 与状态 pause_reason 共用的枚举）：
# 断言文案唯一出处见 breaker_message（tick/decide/执行共用，禁止各自拼 f-string）
BREAKER_MESSAGES = {
    "consecutive_losses": lambda st, cfg: f"连亏 {st.consecutive_losses} 笔（上限 {cfg.max_consecutive_losses}）",
    "daily_loss": lambda st, cfg: f"日亏 {st.daily_loss:.2f} USDC（上限 {cfg.max_daily_loss}）",
}


def circuit_breaker(state: StateView, config: EngineConfig) -> tuple[str, str] | None:
    """熔断判定纯函数（单一事实源）：触发返回 (reason_key, 文案)，否则 None。

    tick 与 decide 共用——曾各自用同一阈值实现一遍（tick 先跑，
    decide 的 PAUSE 分支成死路径），文案还各写各的。
    """
    if state.consecutive_losses >= config.max_consecutive_losses:
        return "consecutive_losses", BREAKER_MESSAGES["consecutive_losses"](state, config)
    if state.daily_loss >= config.max_daily_loss:
        return "daily_loss", BREAKER_MESSAGES["daily_loss"](state, config)
    return None


def decide(config: EngineConfig, state: StateView, market: MarketView, signal: Signal) -> Action:
    """根据当前状态与信号决定下一个动作。

    state: 连续亏损、当日亏损、本窗口是否已下注、是否暂停。
    market: 距窗口结束秒数、目标方向 best ask/bid、当前持仓、挂单。
    """
    # 熔断优先于一切交易动作；人工暂停时不产生任何交易动作
    # （判定与文案与 tick 共用 circuit_breaker 单一事实源——tick 先跑故此处
    # 正常序列不可达，保留为决策引擎防守兜底，不再自写一版阈值）
    if state.paused:
        return Action(ActionType.SKIP)
    trip = circuit_breaker(state, config)
    if trip is not None:
        return Action(ActionType.PAUSE, reason=trip[0])

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

    return _maybe_enter(config, signal, market.best_ask, market.remaining_sec, market.elapsed_sec)


def _manage_position(config: EngineConfig, position: Position, best_bid: float | None, remaining_sec: int) -> Action:
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
    return Action(ActionType.SKIP)


def _maybe_enter(config: EngineConfig, signal: Signal, best_ask: float | None,
                 remaining_sec: int, elapsed_sec: int = 0) -> Action:
    if signal.direction is Direction.SKIP:
        return Action(ActionType.SKIP)
    # 开仓延迟：市场开始后 N 秒内不开仓（观察早期波动，避免开盘瞬间噪声信号；0 = 关闭）
    if config.open_delay_sec > 0 and elapsed_sec < config.open_delay_sec:
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
