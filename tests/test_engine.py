"""决策引擎测试。

测试接缝：decide(config, state, market, signal) → Action。
所有输入为纯数据（fake 适配器），不碰网络/真钱/真实时间。
"""

from dataclasses import replace

from pmbot.config import Config
from pmbot.engine import decide
from pmbot.types import ActionType, Direction, MarketView, PendingOrder, Position, Signal, StateView

# 与默认配置一致的固定配置，方便断言
CFG = Config(
    strategy="kronos",
    symbols=["BTC"],
    market_interval="15m",
    amount_per_trade=1,
    p_up_buy=0.60,
    p_down_buy=0.40,
    cancel_before_end_sec=180,
    exit_loss_before_end_sec=30,
    hold_until_end_sec=60,
    take_profit=0.30,
    take_profit_max=0.95,
    stop_loss=0.20,
    time_stop_min=10,
    max_consecutive_losses=10,
    max_daily_loss=10,
    max_klines=2048,
    model_variant="kronos-mini",
    sample_count=20,
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
):
    return MarketView(
        remaining_sec=remaining_sec,
        best_ask=best_ask,
        best_bid=best_bid,
        position=position,
        pending_order=pending_order,
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


def test_time_stop_after_ten_minutes_not_profitable():
    # 入场时距结束 800s，现在距结束 199s（已过 601s > 600s），且未盈利
    market = make_market(
        remaining_sec=199,
        best_bid=0.40,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SELL
    assert action.reason == "time_stop"


def test_no_time_stop_when_profitable():
    # 已超 10 分钟（entered=800 → remaining=199，elapsed=601），但 bid 高于入场价 → 不触发
    market = make_market(
        remaining_sec=199,
        best_bid=0.50,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP


def test_no_time_stop_before_ten_minutes():
    # 入场时距结束 800s，现在距结束 250s（已过 550s < 600s），未到 10 分钟 → 不触发
    market = make_market(
        remaining_sec=250,
        best_bid=0.40,
        position=make_position(),
    )
    action = decide(CFG, make_state(), market, make_signal("skip", 0.5))
    assert action.type is ActionType.SKIP


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
