"""策略决策引擎。

纯函数 decide(config, state, market, signal) → Action：
所有策略参数从配置注入，不写死数字；外部（数据/执行/时钟）均为注入状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from pmbot.config import EngineConfig


@dataclass(frozen=True)
class AutoTuneOverride:
    """自适应调参覆盖（auto_tune 生成）:完整覆盖值——字段恒有值（无调整时为
    对应 config 默认），消费者直读，不做 None→config 回落（回落收口在 tune()）。"""

    max_entry_price: float = 0.0
    take_profit: float = 0.0
from pmbot.exit_rules import position_exit_levels
from pmbot.types import Action, ActionType, Direction, MarketView, Position, Signal, StateView


# 熔断原因 key（Action.reason 与状态 pause_reason 共用的枚举）：
# 断言文案唯一出处见 BREAKER_MESSAGES（tick/decide/执行共用，禁止各自拼 f-string）
BREAKER_MESSAGES = {
    "consecutive_losses": lambda st, cfg: f"连亏 {st.consecutive_losses} 笔（上限 {cfg.max_consecutive_losses}）",
    "daily_loss": lambda st, cfg: f"日亏 {st.daily_loss:.2f} USDC（上限 {cfg.max_daily_loss}）",
}


def live_delta_pct(price: float, baseline: float) -> float:
    """窗口起点至今 Binance 实时移动百分比（单位锁定：0.5 = +0.5%）。

    与 signal_contradicted 的 skip_pct 单位一致（单一书写点，曾散落
    main_loop._live_delta_pct 的注释与 engine 对比处隐式约定）。
    """
    return (price - baseline) / baseline * 100.0


def signal_contradicted(direction: Direction, live_delta_pct: float | None, skip_pct: float) -> bool:
    """方向一致性过滤（单一事实源）：信号方向与 Binance 实时移动大幅矛盾。

    live_delta_pct: 窗口起点至今 Binance 实时移动百分比（如 0.5 = +0.5%）；
    None（无实时价/信号缺基线）→ False 不过滤（宁缺毋滥，不因数据缺失误杀）。
    skip_pct: 矛盾阈值百分比（0 = 关闭）。

    依据：Polymarket Up/Down 结算 = 窗口终点价 vs 窗口起点价；live_delta 是
    结算目标的**部分实现信息**。但 5m 窗口内价格会往返，只能大幅矛盾时跳过，
    不能线性否决模型（阈值回测标定，默认关）。
    """
    if skip_pct <= 0 or live_delta_pct is None:
        return False
    if direction is Direction.UP and live_delta_pct <= -skip_pct:
        return True
    if direction is Direction.DOWN and live_delta_pct >= skip_pct:
        return True
    return False


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


def decide(config: EngineConfig, state: StateView, market: MarketView, signal: Signal,
            now_sec: int | None = None,
            override: AutoTuneOverride | None = None) -> Action:
    """根据当前状态与信号决定下一个动作。

    state: 连续亏损、当日亏损、本窗口是否已下注、是否暂停、建仓冷却截止。
    market: 距窗口结束秒数、目标方向 best ask/bid、当前持仓、挂单。
    now_sec: 墙钟秒（冷却判定注入，纯函数不自行取时）；None = 不做冷却判断。
    override: 自适应调参结果（auto_tune），字段 None 时退化为 config 值。
    """
    # 熔断优先于一切交易动作；人工暂停时不产生任何交易动作
    # （判定与文案与 tick 共用 circuit_breaker 单一事实源——tick 先跑故此处
    # 正常序列不可达，保留为决策引擎防守兜底，不再自写一版阈值）
    if state.paused:
        return Action(ActionType.SKIP)
    # 盘口无报价建仓失败冷却：本窗口冷却期内不再决策建仓（防缺失盘口每秒重试）；
    # now 由调用方注入（与 elapsed_sec 分流同模式），置 None 时跳过判定
    if state.retry_until_sec is not None and now_sec is not None and now_sec < state.retry_until_sec:
        return Action(ActionType.SKIP)
    trip = circuit_breaker(state, config)
    if trip is not None:
        return Action(ActionType.PAUSE, reason=trip[0])

    position = market.position
    if position is not None:
        return _manage_position(config, position, market.best_bid, market.remaining_sec,
                                override=override)

    pending = market.pending_order
    if pending is not None:
        # 挂单未成交，接近窗口结束则撤单
        if market.remaining_sec <= config.cancel_before_end_sec:
            return Action(ActionType.CANCEL)
        return Action(ActionType.SKIP)

    if state.window_bet_placed:
        # 每窗口每标的最多一注
        return Action(ActionType.SKIP)

    return _maybe_enter(config, signal, market.best_ask, market.remaining_sec,
                        market.elapsed_sec, market.live_delta_pct, override=override)


def _manage_position(config: EngineConfig, position: Position, best_bid: float | None, remaining_sec: int,
                     override: AutoTuneOverride | None = None) -> Action:
    if best_bid is None:
        return Action(ActionType.SKIP)
    # 止盈止损：共享 exit_rules 单一事实源（回测/面板同公式）
    # auto_tune 完整覆盖值：override 恒有值（无调整 = config 默认），直读不回落
    tp, sl = position_exit_levels(
        position.entry_price, override.take_profit if override else config.take_profit,
        config.stop_loss,
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
                 remaining_sec: int, elapsed_sec: int = 0,
                 live_delta_pct: float | None = None,
                 override: AutoTuneOverride | None = None) -> Action:
    if signal.direction is Direction.SKIP:
        return Action(ActionType.SKIP)
    # 开仓延迟：市场开始后 N 秒内不开仓（观察早期波动，避免开盘瞬间噪声信号；0 = 关闭）
    if config.open_delay_sec > 0 and elapsed_sec < config.open_delay_sec:
        return Action(ActionType.SKIP)
    # 窗口结束前 N 秒禁止买入（中途启动时避免窗口末仓）
    if remaining_sec <= config.no_entry_before_end_sec:
        return Action(ActionType.SKIP)
    # 方向一致性过滤：信号方向与 Binance 实时移动大幅矛盾（如信号 UP 但实时已跌超阈值）
    # → 跳过入场（模型窗口起点预测被实时走势证伪，市价追单大概率高位接盘）；默认关
    if signal_contradicted(signal.direction, live_delta_pct, config.contradiction_skip_pct):
        return Action(ActionType.SKIP, reason="contradiction")
    # 入场价上限：盘口 ask 高于此价不入场（追高仓位历史净亏；0 = 关闭）。
    # 无报价（None）不拦——执行层缺报价本就放弃建仓。
    # auto_tune 完整覆盖值：override 恒有值（无调整 = config 默认），直读不回落
    cap = override.max_entry_price if override else config.max_entry_price
    if cap > 0 and best_ask is not None and best_ask > cap:
        return Action(ActionType.SKIP, reason="entry_price_cap")
    # 入场价下限：盘口 ask 低于此价不入场（0.45-0.55 五五开档历史净亏 -2.52/31 笔，
    # 该档位无信息优势且买卖价差双向吞噬；0 = 关闭）。
    if config.min_entry_price > 0 and best_ask is not None and best_ask < config.min_entry_price:
        return Action(ActionType.SKIP, reason="entry_price_floor")
    # 市价入场：预测后立即按 1 USDC 目标买入（份额=金额/盘口价，可小数，无 5 股限制）
    if signal.direction is Direction.UP and signal.p_up > config.p_up_buy:
        return Action(ActionType.PLACE_MARKET, direction=Direction.UP, amount=config.amount_per_trade)
    if signal.direction is Direction.DOWN and signal.p_up < config.p_down_buy:
        return Action(ActionType.PLACE_MARKET, direction=Direction.DOWN, amount=config.amount_per_trade)
    return Action(ActionType.SKIP)
