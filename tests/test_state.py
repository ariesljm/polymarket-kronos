"""TradeState 测试：窗口切换、结算盈亏、熔断计数、持久化。"""

import json
from pathlib import Path

import pytest

from pmbot.state import Position, StateStore, TradeState
from pmbot.types import Direction, PendingOrder


def make_state(**kw):
    defaults = dict(
        symbol="BTC",
        window_start=1_000_000,
        window_bet_placed=False,
        signal=None,
        position=None,
        pending_order=None,
        consecutive_losses=0,
        daily_loss=0.0,
        paused=False,
        last_day="",
    )
    defaults.update(kw)
    return TradeState(**defaults)


def make_position(direction=Direction.UP, entry_price=0.45, size=2.0):
    return Position(
        direction=direction,
        entry_price=entry_price,
        size=size,
        entered_remaining_sec=800,
        window_start=1_000_000,
    )


def make_pending(direction=Direction.UP, price=0.45, order_id="o1"):
    return PendingOrder(
        direction=direction, price=price, size=5.0, order_id=order_id, created_sec=1
    )


# ---- 窗口切换 ----

def test_new_window_resets_bet_and_pending():
    st = make_state(window_bet_placed=True, pending_order=make_pending())
    st.roll_window(2_000_000)
    assert st.window_start == 2_000_000
    assert st.window_bet_placed is False
    assert st.pending_order is None


def test_same_window_no_reset():
    st = make_state(window_bet_placed=True)
    st.roll_window(1_000_000)
    assert st.window_bet_placed is True


def test_roll_window_keeps_position():
    # 持仓跨窗口？不应该发生（窗口结束必结算），防御：roll 不清 position
    st = make_state(position=make_position())
    st.roll_window(2_000_000)
    assert st.position is not None


# ---- 结算盈亏 ----

def test_settle_win():
    st = make_state(position=make_position())
    pnl = st.close_position(settle_price=1.0)
    # size 2.0 * (1.0 - 0.45) = 1.1
    assert pnl == pytest.approx(1.1)
    assert st.position is None
    assert st.consecutive_losses == 0
    assert st.daily_loss == 0.0


def test_settle_loss_updates_circuit_breaker():
    st = make_state(position=make_position())
    pnl = st.close_position(settle_price=0.0)
    # size 2.0 * (0.0 - 0.45) = -0.9（全损）
    assert pnl == pytest.approx(-0.9)
    assert st.consecutive_losses == 1
    assert st.daily_loss == pytest.approx(0.9)  # 当日累计亏损（正数）


def test_take_profit_close():
    st = make_state(position=make_position())
    pnl = st.close_position(settle_price=0.80)
    # 2.0 * (0.80 - 0.45) = 0.7
    assert pnl == pytest.approx(0.7)
    assert st.consecutive_losses == 0


def test_stop_loss_close():
    st = make_state(position=make_position())
    pnl = st.close_position(settle_price=0.30)
    # 2.0 * (0.30 - 0.45) = -0.3
    assert pnl == pytest.approx(-0.3)
    assert st.consecutive_losses == 1


# ---- 日亏损重置 ----

def test_daily_loss_resets_on_new_day():
    st = make_state(daily_loss=8.0, last_day="2026-08-14")
    st.roll_day("2026-08-15")
    assert st.daily_loss == 0.0
    assert st.last_day == "2026-08-15"


def test_daily_loss_kept_same_day():
    st = make_state(daily_loss=8.0, last_day="2026-08-14")
    st.roll_day("2026-08-14")
    assert st.daily_loss == pytest.approx(8.0)


# ---- 持久化 ----

def test_status_roundtrip(tmp_path):
    st = make_state(
        window_bet_placed=True,
        consecutive_losses=3,
        daily_loss=2.5,
        paused=True,
        position=make_position(),
        pending_order=make_pending(),
    )
    store = StateStore(Path(tmp_path) / "status.json")
    store.save(st)
    loaded = store.load()
    assert loaded.window_start == st.window_start
    assert loaded.consecutive_losses == 3
    assert loaded.daily_loss == pytest.approx(2.5)
    assert loaded.paused is True
    assert loaded.position.direction is Direction.UP
    assert loaded.pending_order == make_pending()


def test_load_missing_status_returns_none(tmp_path):
    assert StateStore(Path(tmp_path) / "nope.json").load() is None


def test_trades_log_appends_csv(tmp_path):
    st = make_state()
    store = StateStore(Path(tmp_path) / "status.json", Path(tmp_path) / "trades.csv")
    store.log_trade(
        st,
        window_start=1_000_000,
        direction=Direction.UP,
        entry_price=0.45,
        exit_price=1.0,
        size=2.0,
        pnl=1.1,
        reason="settle",
    )
    store.log_trade(
        st,
        window_start=2_000_000,
        direction=Direction.DOWN,
        entry_price=0.45,
        exit_price=0.0,
        size=2.0,
        pnl=-0.9,
        reason="settle",
    )
    lines = store.trades_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # 表头 + 2 行
    assert "settle" in lines[1]
