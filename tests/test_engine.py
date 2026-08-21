"""决策引擎测试。

测试接缝：decide(config, state, market, signal) → Action。
所有输入为纯数据（fake 适配器），不碰网络/真钱/真实时间。
"""

from dataclasses import replace

from pmbot.config import EngineConfig
from pmbot.engine import decide
from pmbot.types import ActionType, Direction, MarketView, PendingOrder, Position, Signal, StateView

# 与默认配置一致的固定引擎配置，方便断言
CFG = EngineConfig(
    amount_per_trade=1,
    p_up_buy=0.60,
    p_down_buy=0.40,
    cancel_before_end_sec=180,
    exit_loss_before_end_sec=30,
    hold_until_end_sec=60,
    take_profit=0.30,
    take_profit_max=0.95,
    stop_loss=0.20,
    max_consecutive_losses=10,
    max_daily_loss=10,
)


def make_state(
    *,
    consecutive_losses=0,
    daily_loss=0.0,
    window_bet_placed=False,
    paused=False,
):
    return StateView(
        consecutive_losses=consecutive_losses,
        daily_loss=daily_loss,
        window_bet_placed=window_bet_placed,
        paused=paused,
    )


def make_position(direction=Direction.UP, entry_price=0.45, entered_remaining_sec=800):
    return Position(
        direction=direction,
        entry_price=entry_price,
        size=5.0,
        entered_remaining_sec=entered_remaining_sec,
        window_start=0,
    )


def make_pending(direction=Direction.UP, price=0.45):
    return PendingOrder(
        direction=direction, price=price, size=5.0, order_id="o1", created_sec=0
    )


def make_market(
    *,
    remaining_sec=800,
    best_ask=0.42,
    best_bid=None,
    position=None,
    pending_order=None,
    elapsed_sec=800,
):
    return MarketView(
        remaining_sec=remaining_sec,
        best_ask=best_ask,
        best_bid=best_bid,
        position=position,
        pending_order=pending_order,
        elapsed_sec=elapsed_sec,
    )


def make_signal(direction, p_up):
    return Signal(direction=Direction(direction), p_up=p_up)


# ---- 信号阈值边界 ----

def test_buy_up_only_when_p_up_above_threshold():
    # 恰好 0.60 不买（严格大于），0.61 买
    assert decide(CFG, make_state(), make_market(), make_signal("up", 0.60)).type is ActionType.SKIP
    action = decide(CFG, make_state(), make_market(), make_signal("up", 0.61))
    assert action.type is ActionType.PLACE_MARKET
    assert action.direction is Direction.UP
    assert action.amount == 1


def test_buy_down_only_when_p_up_below_threshold():
    assert decide(CFG, make_state(), make_market(), make_signal("down", 0.40)).type is ActionType.SKIP
    action = decide(CFG, make_state(), make_market(), make_signal("down", 0.39))
    assert action.type is ActionType.PLACE_MARKET
    assert action.direction is Direction.DOWN


def test_midband_skips():
    action = decide(CFG, make_state(), make_market(), make_signal("up", 0.50))
    assert action.type is ActionType.SKIP


def test_skip_signal_skips():
    action = decide(CFG, make_state(), make_market(), make_signal("skip", 0.50))
    assert action.type is ActionType.SKIP


# ---- 入场：市价买入（预测后立即按 1 USDC 目标入场） ----

def test_entry_ignores_limit_price():
    """市价入场与限价无关：ask 高低都直接买。"""
    market = make_market(best_ask=0.55)
    action = decide(CFG, make_state(), market, make_signal("up", 0.70))
    assert action.type is ActionType.PLACE_MARKET
    assert action.price is None
    assert action.amount == 1


def test_entry_market_when_ask_at_or_below_limit_price():
    market = make_market(best_ask=0.45)
    action = decide(CFG, make_state(), market, make_signal("up", 0.70))
    assert action.type is ActionType.PLACE_MARKET


# ---- 挂单与撤单 ----

def test_wait_while_pending_order_open():
    market = make_market(pending_order=make_pending())
    action = decide(CFG, make_state(), market, make_signal("up", 0.70))
    assert action.type is ActionType.SKIP


def test_cancel_when_close_to_window_end():
    market = make_market(
        remaining_sec=180,
        pending_order=make_pending(),
    )
    action = decide(CFG, make_state(), market, make_signal("up", 0.70))
    assert action.type is ActionType.CANCEL


def test_pending_order_not_cancelled_with_time_left():
    market = make_market(
        remaining_sec=200,
        pending_order=make_pending(),
    )
    action = decide(CFG, make_state(), market, make_signal("up", 0.70))
    assert action.type is ActionType.SKIP


# ---- 止盈 ----

def test_take_profit_when_bid_reaches_target():
    market = make_market(
        best_bid=0.80,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "take_profit"


def test_no_sell_below_take_profit():
    # 0.50 < 止盈 0.585（0.45×1.3）→ 不卖
    market = make_market(
        best_bid=0.50,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP


# ---- 止损 ----

def test_stop_loss_when_bid_crashes():
    market = make_market(
        best_bid=0.30,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "stop_loss"


# ---- 窗口结束前强制平仓 ----

def test_window_end_forces_exit_when_lossing():
    # 剩余 30s ≤ exit_loss_before_end_sec=30，未到止盈且未触止损线（bid 0.40 > SL 0.30）→ 亏损离场
    market = make_market(
        remaining_sec=30,
        best_bid=0.40,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "window_end"


def test_window_end_holds_profitable_to_settlement():
    # 剩余 30s，盈利（0.50 > 入场 0.45）但未达止盈 0.585 → 持有到结算（不主动平仓）
    market = make_market(
        remaining_sec=30,
        best_bid=0.50,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP


def test_percent_tp_sl_scales_with_entry():
    # 入场 0.20：止盈 = 0.20×1.3 = 0.26，止损 = 0.20×0.8 = 0.16
    action = decide(CFG, make_state(), make_market(best_bid=0.26, position=make_position(entry_price=0.20)),
                    make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "take_profit"
    action = decide(CFG, make_state(), make_market(best_bid=0.25, position=make_position(entry_price=0.20)),
                    make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP
    action = decide(CFG, make_state(), make_market(best_bid=0.16, position=make_position(entry_price=0.20)),
                    make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "stop_loss"


def test_take_profit_capped_by_max_price():
    """入场价高时百分比止盈价 >1 → 按 take_profit_max 封顶止盈。"""
    # 入场 0.80：0.80×1.3 = 1.04 > 1 → 封顶 0.95
    action = decide(CFG, make_state(),
                    make_market(best_bid=0.95, position=make_position(entry_price=0.80)),
                    make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "take_profit"
    # 0.94 < 0.95 未达封顶止盈线 → 继续持有
    action = decide(CFG, make_state(),
                    make_market(best_bid=0.94, position=make_position(entry_price=0.80)),
                    make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP
    # 正常范围入场不受封顶影响：0.50×1.3 = 0.65 < 0.95
    action = decide(CFG, make_state(),
                    make_market(best_bid=0.65, position=make_position(entry_price=0.50)),
                    make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "take_profit"


def test_window_end_take_profit_wins_first():
    # 剩余 30s 且已达止盈 → 止盈优先
    market = make_market(
        remaining_sec=30,
        best_bid=0.80,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "take_profit"


def test_no_window_end_before_threshold():
    # 剩余 90s > exit_loss_before_end_sec=30 且 > hold_until_end_sec=60，未到时间止损 → 继续持有
    market = make_market(
        remaining_sec=90,
        best_bid=0.40,
        position=make_position(entered_remaining_sec=300),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP


# ---- 熔断 ----

def test_pause_on_consecutive_losses():
    action = decide(CFG, make_state(consecutive_losses=10), make_market(), make_signal("up", 0.70))
    assert action.type is ActionType.PAUSE


def test_no_pause_below_consecutive_loss_limit():
    action = decide(CFG, make_state(consecutive_losses=9), make_market(), make_signal("up", 0.70))
    assert action.type is ActionType.PLACE_MARKET


def test_pause_on_daily_loss():
    action = decide(CFG, make_state(daily_loss=10.0), make_market(), make_signal("up", 0.70))
    assert action.type is ActionType.PAUSE


def test_no_pause_below_daily_loss_limit():
    action = decide(CFG, make_state(daily_loss=9.9), make_market(), make_signal("up", 0.70))
    assert action.type is ActionType.PLACE_MARKET


def test_pause_overrides_everything():
    # 即使市场有止盈机会，熔断也必须优先
    market = make_market(
        best_bid=0.80,
        position=make_position(),
    )
    action = decide(CFG, make_state(consecutive_losses=10), market, make_signal("skip", 0.5))
    assert action.type is ActionType.PAUSE


def test_manual_pause_blocks_trading():
    # 人工暂停：即使有入场信号也不产生交易动作
    action = decide(CFG, make_state(paused=True), make_market(), make_signal("up", 0.70))
    assert action.type is ActionType.SKIP


# ---- 单窗口一注 ----

def test_no_second_bet_in_same_window():
    market = make_market(best_ask=0.40)
    state = make_state(window_bet_placed=True)
    action = decide(CFG, state, market, make_signal("up", 0.70))
    assert action.type is ActionType.SKIP


def test_no_entry_near_window_end():
    """窗口结束前 no_entry_before_end_sec 秒内禁止买入（中途启动场景）。"""
    cfg = replace(CFG, no_entry_before_end_sec=60)
    market = make_market(remaining_sec=59)  # 不足 60s → 不买
    a = decide(cfg, make_state(), market, make_signal(Direction.UP, p_up=0.95))
    assert a.type is ActionType.SKIP
    market = make_market(remaining_sec=61)  # 足够 → 正常买入
    a = decide(cfg, make_state(), market, make_signal(Direction.UP, p_up=0.95))
    assert a.type is ActionType.PLACE_MARKET


def test_no_entry_gate_off_when_zero():
    """no_entry_before_end_sec=0 时关闭禁买（任何剩余时间都可买入）。"""
    cfg = replace(CFG, no_entry_before_end_sec=0)
    a = decide(cfg, make_state(), make_market(remaining_sec=1), make_signal(Direction.UP, p_up=0.95))
    assert a.type is ActionType.PLACE_MARKET


# ---- 开仓延迟：市场开始后 N 秒内不开仓 ----

def test_open_delay_blocks_entry_before_elapsed():
    """open_delay_sec 内窗口已进行秒数不足 → 不开仓；达到 → 正常买入。"""
    cfg = replace(CFG, open_delay_sec=60)
    a = decide(cfg, make_state(), make_market(elapsed_sec=30), make_signal(Direction.UP, p_up=0.95))
    assert a.type is ActionType.SKIP
    a = decide(cfg, make_state(), make_market(elapsed_sec=60), make_signal(Direction.UP, p_up=0.95))
    assert a.type is ActionType.PLACE_MARKET


def test_open_delay_off_when_zero():
    """open_delay_sec=0（默认）关闭延迟：窗口刚开始即可买。"""
    a = decide(CFG, make_state(), make_market(elapsed_sec=0), make_signal(Direction.UP, p_up=0.95))
    assert a.type is ActionType.PLACE_MARKET


# ---- 窗口末拆分：亏损离场 / 盈利持有 两个独立阈值 ----

def test_exit_loss_and_hold_until_are_independent():
    """exit_loss_before_end_sec 与 hold_until_end_sec 独立配置。"""
    # 亏损但未到亏损离场阈值（remaining 45 > exit_loss 30）→ 不强制离场
    action = decide(CFG, make_state(), make_market(
        remaining_sec=45, best_bid=0.40, position=make_position(entry_price=0.45, entered_remaining_sec=100)),
        make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP
    # 亏损且到阈值（remaining 30 ≤ exit_loss 30）→ 离场
    action = decide(CFG, make_state(), make_market(
        remaining_sec=30, best_bid=0.40, position=make_position(entry_price=0.45, entered_remaining_sec=100)),
        make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "window_end"


def test_hold_until_end_sec_holds_profitable_earlier():
    """盈利持有阈值更大：remaining 40（≤ hold_until 60、> exit_loss 30）盈利 → 持有到结算。"""
    action = decide(CFG, make_state(), make_market(
        remaining_sec=40, best_bid=0.50, position=make_position(entry_price=0.45, entered_remaining_sec=100)),
        make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP  # 盈利：持有，不主动平仓


def test_exit_loss_disabled_holds_loss_to_settlement():
    """exit_loss_before_end_sec=0（关闭）时：窗口末亏损也不平仓，持有到结算。"""
    cfg = replace(CFG, exit_loss_before_end_sec=0)
    # 亏损（0.40 < 0.45）但在止损线上方（sl=0.45×0.8=0.36）→ 持有到结算
    action = decide(cfg, make_state(), make_market(
        remaining_sec=10, best_bid=0.40, position=make_position(entry_price=0.45, entered_remaining_sec=100)),
        make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP
    # 跌破止损线 → 仍触发 stop_loss
    action = decide(cfg, make_state(), make_market(
        remaining_sec=10, best_bid=0.15, position=make_position(entry_price=0.45, entered_remaining_sec=100)),
        make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "stop_loss"


# ---- 熔断判定纯函数（候选 5：tick/decide 共用单一事实源） ----

def test_circuit_breaker_pure_function():
    """熔断判定纯函数：阈值/文案单一出处，tick 与 decide 共用。"""
    from pmbot.engine import circuit_breaker

    assert circuit_breaker(make_state(consecutive_losses=5), CFG) is None  # 未到阈值
    trip = circuit_breaker(make_state(consecutive_losses=10), CFG)  # 恰好阈值
    assert trip is not None
    key, message = trip
    assert key == "consecutive_losses"
    assert "连亏 10 笔" in message and "上限 10" in message
    trip = circuit_breaker(make_state(daily_loss=10.5), CFG)
    assert trip is not None and trip[0] == "daily_loss"
    assert "日亏 10.50 USDC" in trip[1]


def test_decide_pause_uses_same_breaker():
    """decide 的 PAUSE 分支文案/阈值与 tick 共用（不再自写一版）。"""
    action = decide(CFG, make_state(consecutive_losses=10), make_market(),
                    make_signal("up", 0.95))
    assert action.type is ActionType.PAUSE
    assert action.reason == "consecutive_losses"
