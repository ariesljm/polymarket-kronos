"""ExecutionDispatcher 平仓记账：dry-run 手续费模拟的正确性。"""

from types import SimpleNamespace

from pmbot.engine import AutoTuneOverride
from pmbot.execution_dispatcher import ExecutionDispatcher
from pmbot.executor_protocols import MIN_TICK_PRICE
from pmbot.state import TradeState
from pmbot.types import Action, ActionType, Direction, Fill, Position


def make_dispatcher(*, dry_run=True, fee=0.03, trades=None, statuses=None,
                    book=None, trade=None, min_entry=0.0, max_entry=0.0,
                    auto_override=None):
    """标准测试替身：store.log_trade / save_status 用真列表记录调用。"""
    trades = [] if trades is None else trades
    statuses = [] if statuses is None else statuses

    def log_trade(st, **kw):
        trades.append(dict(kw))

    return ExecutionDispatcher(
        state=TradeState(symbol="BTC", mode="dry-run" if dry_run else "live"),
        trade=trade if trade is not None else SimpleNamespace(),
        book=book if book is not None else SimpleNamespace(),
        store=SimpleNamespace(log_trade=log_trade),
        dry_run=dry_run,
        step_sec=300,
        save_status=lambda: statuses.append(1),
        taker_fee_pct=fee,
        min_entry_price=min_entry,
        max_entry_price=max_entry,
        auto_override=auto_override,
    ), trades


def make_pos(size=1.0, entry=0.50, window=1780000000):
    return Position(
        direction=Direction.UP,
        entry_price=entry,
        size=size,
        entered_remaining_sec=300,
        window_start=window,
    )


def test_dry_run_fee_deducted_both_sides():
    """dry-run + fee：买入成本含费、卖出收入扣费，期望 = size×[exit(1−f) − entry(1+f)]。"""
    disp, trades = make_dispatcher(dry_run=True, fee=0.03)
    pos = make_pos(size=2.0, entry=0.50)
    pnl = disp.close_position(pos, exit_price=0.80, reason="take_profit")
    # 2.0 × [0.80×0.97 − 0.50×1.03] = 2.0 × [0.776 − 0.515] = 0.522
    assert abs(pnl - 0.522) < 1e-9
    assert abs(trades[0]["pnl"] - 0.522) < 1e-9  # 记账与返回值同源


def test_dry_run_zero_fee_keeps_theoretical_spread():
    """fee=0（默认/未配置）保持旧理论价差口径：size×(exit − entry)。"""
    disp, trades = make_dispatcher(dry_run=True, fee=0.0)
    pos = make_pos(size=2.0, entry=0.50)
    pnl = disp.close_position(pos, exit_price=0.80, reason="take_profit")
    assert abs(pnl - 0.60) < 1e-9  # 2.0 × 0.30


def test_live_never_deducts_fee():
    """实盘不受 fee 参数影响：pnl 由真实余额差（sell_proceeds）给出，费用已内含。"""
    disp, trades = make_dispatcher(dry_run=False, fee=0.03)
    pos = make_pos(size=2.0, entry=0.50)
    # 实盘 close_position 直接调 state.close_position（actual_pnl=None → 理论价差占位）；
    # 真实路径经 settler proceeds/_exec_sell sell_proceeds 显式传入，此处验证 fee 不回退到扣费分支
    pnl = disp.close_position(pos, exit_price=0.80, reason="sell")
    assert abs(pnl - 0.60) < 1e-9  # 与 fee=0 一致，fee 分支不生效


def test_settle_proceeds_have_priority_no_fee():
    """结算兑付（proceeds 显式传入）优先使用真实兑付额，不叠加手续费模拟（结算无 taker 费）。"""
    disp, trades = make_dispatcher(dry_run=True, fee=0.03)
    pos = make_pos(size=1.0, entry=0.40)
    # 结算兑付 1.0（Up 赢）：pnl = proceeds − size×entry = 0.60，与 fee 无关
    pnl = disp.close_position(pos, exit_price=1.0, reason="settle", proceeds=1.0)
    assert abs(pnl - 0.60) < 1e-9


def test_fee_loss_counts_towards_breaker():
    """扣费后亏损仍计入连亏/日亏（熔断口径不因费用模拟改变）。"""
    disp, _ = make_dispatcher(dry_run=True, fee=0.03)
    disp.close_position(make_pos(size=1.0, entry=0.50), exit_price=0.20, reason="stop_loss")
    st = disp.state
    assert st.consecutive_losses == 1
    assert st.daily_loss > 0.30  # 0.5×1.03 − 0.2×0.97 = 0.321


# ---- 执行层入场价闸门（决策后盘口二次校验）：与引擎共用 entry_gate ----

def _book(ask):
    return SimpleNamespace(best_ask=lambda token_id, size=1.0: ask)


def _market():
    return SimpleNamespace(window_start=1780000000, yes_token_id="Y", no_token_id="N")


def _place():
    return Action(ActionType.PLACE_MARKET, direction=Direction.UP, amount=1.0)


def test_exec_passes_gate_cap_as_protection_price():
    """闸门上限透传为市价单保护价：下单瞬间盘口若已极化到上限之上，FOK 直接拒单
    （宁错过不追高），而非照极化价成交。"""
    seen = {}

    def buy(token_id, amount, max_price=None):
        seen["max_price"] = max_price
        return Fill(order_id="o1", avg_price=0.50, filled_size=2.0)

    disp, _ = make_dispatcher(book=_book(0.50), trade=SimpleNamespace(market_buy=buy),
                              min_entry=0.30, max_entry=0.60)
    disp.state.window_start = 1780000000
    disp.execute(_place(), _market(), 1780000000)
    assert seen["max_price"] == 0.60


def test_exec_no_cap_passes_no_protection_price():
    """上限未配置（0）→ 不传保护价（None），保持 SDK 自动算吃穿价的行为。"""
    seen = {}

    def buy(token_id, amount, max_price=None):
        seen["max_price"] = max_price
        return Fill(order_id="o1", avg_price=0.50, filled_size=2.0)

    disp, _ = make_dispatcher(book=_book(0.50), trade=SimpleNamespace(market_buy=buy))
    disp.state.window_start = 1780000000
    disp.execute(_place(), _market(), 1780000000)
    assert seen["max_price"] is None


def test_exec_zero_cap_rejects_entry_and_skips_order():
    """auto_tune 收窄上限到 0 → 执行层按 entry_price_cap 拦下，不下单。

    回归（2026-09-12 实盘事故）：闸门曾把 0.0 当成「不限制」，把盘口 0.98 的
    仓位以市价单送出（且保护价为 None），远超任何入场上限。
    """
    calls = []

    def buy(token_id, amount, max_price=None):
        calls.append(max_price)
        return Fill(order_id="o1", avg_price=0.50, filled_size=2.0)

    disp, _ = make_dispatcher(book=_book(0.98), trade=SimpleNamespace(market_buy=buy),
                              min_entry=0.30, max_entry=0.60,
                              auto_override=lambda: AutoTuneOverride(max_entry_price=0.0))
    disp.state.window_start = 1780000000
    disp.execute(_place(), _market(), 1780000000)
    assert calls == []  # 未下单
    assert disp.state.retry_until_sec > 1780000000  # 走冷却，不每 tick 重试


def test_exec_sell_passes_min_tick_as_protection_price():
    """平仓市价卖传最小 tick 作保护价：接受任何合法价（平仓只求成交），
    与 SDK 自动算吃穿价等价，但省掉下单前的一次 get_order_book REST。"""
    seen = {}

    def sell(token_id, size, min_price=None):
        seen["min_price"] = min_price
        return Fill(order_id="s1", avg_price=0.80, filled_size=2.0)

    disp, _ = make_dispatcher(trade=SimpleNamespace(market_sell=sell))
    disp.state.position = make_pos(size=2.0)
    disp.state.window_start = 1780000000
    disp.execute(Action(ActionType.SELL, reason="take_profit"), _market(), 1780000000)
    assert seen["min_price"] == MIN_TICK_PRICE


def test_subscribe_sampler_warms_token_metadata():
    """窗口订阅时预热下单客户端 token 元数据（每窗口 token 新 → 否则每笔首单
    都要在下单关键路径上多花 2 次 REST）。"""
    warmed = []
    sampler = SimpleNamespace(subscribe=lambda tokens, direction_map=None: None)
    trade = SimpleNamespace(warmup=lambda tokens: warmed.append(list(tokens)))
    disp, _ = make_dispatcher(trade=trade, book=SimpleNamespace(sampler=sampler))
    disp.subscribe_sampler(_market())
    assert warmed == [["Y", "N"]]


def test_exec_gate_rejects_ask_above_cap():
    """决策后盘口极化越过上限 → 拦截，不调用下单。"""
    calls = []
    trade = SimpleNamespace(market_buy=lambda *a, **k: calls.append(a))
    disp, _ = make_dispatcher(book=_book(0.75), trade=trade, max_entry=0.60)
    disp.execute(_place(), _market(), 1780000000)
    assert calls == []
    assert disp.state.retry_until_sec == 1780000000 + 10  # 同一冷却，不每 tick 重试


def test_exec_gate_rejects_ask_below_floor():
    """决策后盘口走低跌破下限 → 拦截（执行层自此与引擎对称校验下限）。"""
    calls = []
    trade = SimpleNamespace(market_buy=lambda *a, **k: calls.append(a))
    disp, _ = make_dispatcher(book=_book(0.25), trade=trade, min_entry=0.30, max_entry=0.60)
    disp.execute(_place(), _market(), 1780000000)
    assert calls == []
    assert disp.state.retry_until_sec == 1780000000 + 10


def test_exec_gate_override_narrows_cap():
    """auto_tune 覆盖收窄上限：config max=0.60 但 override=0.40 → ask 0.50 被拦。"""
    calls = []
    trade = SimpleNamespace(market_buy=lambda *a, **k: calls.append(a))
    disp, _ = make_dispatcher(
        book=_book(0.50), trade=trade, max_entry=0.60,
        auto_override=lambda: AutoTuneOverride(max_entry_price=0.40, take_profit=0.95),
    )
    disp.execute(_place(), _market(), 1780000000)
    assert calls == []


def test_exec_gate_passes_in_range_ask():
    """ask 落在区间内 → 放行下单，建立持仓。"""
    trade = SimpleNamespace(
        market_buy=lambda *a, **k: Fill(order_id="o1", avg_price=0.50, filled_size=2.0)
    )
    disp, _ = make_dispatcher(book=_book(0.50), trade=trade, min_entry=0.30, max_entry=0.60)
    disp.state.window_start = 1780000000  # 建仓用状态窗口起点
    disp.execute(_place(), _market(), 1780000000)
    assert disp.state.position is not None
    assert disp.state.window_bet_placed is True
    assert disp.state.retry_until_sec is None