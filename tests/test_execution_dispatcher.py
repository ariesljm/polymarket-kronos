"""ExecutionDispatcher 平仓记账：dry-run 手续费模拟的正确性。"""

from types import SimpleNamespace

from pmbot.execution_dispatcher import ExecutionDispatcher
from pmbot.state import TradeState
from pmbot.types import Direction, Position


def make_dispatcher(*, dry_run=True, fee=0.03, trades=None, statuses=None):
    """标准测试替身：store.log_trade / save_status 用真列表记录调用。"""
    trades = [] if trades is None else trades
    statuses = [] if statuses is None else statuses

    def log_trade(st, **kw):
        trades.append(dict(kw))

    return ExecutionDispatcher(
        state=TradeState(symbol="BTC", mode="dry-run" if dry_run else "live"),
        trade=SimpleNamespace(),
        book=SimpleNamespace(),
        store=SimpleNamespace(log_trade=log_trade),
        dry_run=dry_run,
        step_sec=300,
        save_status=lambda: statuses.append(1),
        taker_fee_pct=fee,
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