"""主循环测试：窗口流程全链路（注入 fake 策略/发现/执行，dry-run 语义）。

时间约定：窗口 W=999900（秒，15m 对齐）；tick 参数为毫秒。
- 入场 tick: 1_000_000_000（窗口开盘后 100s）
- 下一 tick: 1_000_100_000（+100s）
- 结算 tick: 999_900_000 + 900_000 + 60_000 = 1_000_860_000（窗口结束 60s 后）
- 下一窗口: 999_900_000 + 900_000 + 100_000 = 1_000_900_000 → window=1_000_800
"""

from pathlib import Path

import pytest

from pmbot.config import Config
from pmbot.main_loop import TradingLoop
from pmbot.state import Position, TradeState
from pmbot.types import Direction, PendingOrder, Signal

WINDOW_MS = 900_000

CFG = Config(
    strategy="kronos",
    symbols=["BTC"],
    market_interval="15m",
    amount_per_trade=1,
    p_up_buy=0.60,
    p_down_buy=0.40,
    cancel_before_end_sec=180,
    exit_before_end_sec=30,
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


class FakeStrategy:
    def __init__(self, signal, fn=None):
        self._signal = signal
        self._fn = fn

    def generate_signal(self, context=None):
        if self._fn:
            return self._fn(context)
        return self._signal


class FakeDiscovery:
    step_ms = 900_000  # 15m（与测试窗口对齐）

    def __init__(self, market=None, settled=None):
        self.market = market
        self.settled = settled

    def find_current_window(self, symbol, now_ms):
        return self.market

    def find_window(self, symbol, window_start, require_tradable=True):
        return self.settled


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.filled = False
        self.best_bid_value = None
        self.best_ask_value = 0.42

    def place_limit(self, token_id, side, price, size):
        self.calls.append(("place", token_id, side, price, size))
        return "oid-1"

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))
        return True

    def market_sell(self, token_id, size):
        self.calls.append(("sell", token_id, size))
        return self.best_bid_value

    def market_buy(self, token_id, size):
        self.calls.append(("market_buy", token_id, size))
        return "dry-run-mk"

    def best_bid(self, token_id):
        return self.best_bid_value

    def best_ask(self, token_id):
        return self.best_ask_value

    def get_order(self, order_id):
        return {"status": "filled" if self.filled else "live", "orderID": order_id}


def make_market(price=0.5):
    from pmbot.market_discovery import MarketInfo

    return MarketInfo(
        condition_id="c1",
        yes_token_id="YES",
        no_token_id="NO",
        outcome_prices=(price, 1 - price),
        accepting_orders=True,
        question="Bitcoin Up or Down - test",
        end_date="",
        window_start=999_900,
    )


def make_loop(tmp_path, *, state=None, strategy=None, discovery=None, executor=None,
              user_stream=None):
    st = state or TradeState(symbol="BTC", window_start=None)
    return TradingLoop(
        config=CFG,
        symbol="BTC",
        strategy=strategy or FakeStrategy(Signal(Direction.UP, 0.70)),
        discovery=discovery or FakeDiscovery(make_market()),
        executor=executor or FakeExecutor(),
        state=st,
        trades_path=Path(tmp_path) / "trades.csv",
        status_path=Path(tmp_path) / "status.json",
        dry_run=True,
        user_stream=user_stream,
    )


def make_position(direction=Direction.UP, entry=0.45, size=2.0, window=999_900):
    return Position(direction, entry, size, 800, window)


def make_pending(direction=Direction.UP, price=0.45, order_id="oid-1"):
    return PendingOrder(direction=direction, price=price, size=5.0, order_id=order_id, created_sec=1_000_000)


# ---- 入场 ----

def test_strong_signal_buys_market(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(tmp_path, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert ex.calls and ex.calls[0][0] == "market_buy"
    _, token, size = ex.calls[0]
    assert token == "YES"  # up 方向买 yes
    assert abs(size - 1 / 0.42) < 1e-9  # 1 USDC / ask
    assert loop.state.window_bet_placed is True
    assert loop.state.pending_order is None  # 市价单立即成交，无挂单
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.42
    assert abs(loop.state.position.size - 1 / 0.42) < 1e-9


def test_low_ask_enlarges_shares_to_1usdc_target(tmp_path):
    """市价低时份额按 1 USDC 目标放大：ask 0.1 → 10 股。"""
    ex = FakeExecutor()
    ex.best_ask_value = 0.10
    loop = make_loop(tmp_path, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert ex.calls and ex.calls[0][0] == "market_buy"
    _, token, size = ex.calls[0]
    assert token == "YES"
    assert abs(size - 10) < 1e-9  # 1/0.1 = 10 股 ≈ 1 USDC
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.10
    assert abs(loop.state.position.size - 10) < 1e-9
    assert loop.state.window_start == 999_900


def test_weak_signal_skips(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(tmp_path, strategy=FakeStrategy(Signal(Direction.UP, 0.50)), executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert ex.calls == []


def test_no_market_skips_gracefully(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(tmp_path, discovery=FakeDiscovery(None), executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert ex.calls == []


def test_down_signal_buys_no_token(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        strategy=FakeStrategy(Signal(Direction.DOWN, 0.30)),
        executor=ex,
    )
    loop.tick(now_ms=1_000_000_000)
    assert ex.calls[0][1] == "NO"


# ---- 挂单成交 ----

def test_pending_order_fill_creates_position(tmp_path):
    """真实订单（非 dry-run）由 get_order 状态检测成交。"""
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="oid-1"),
    )
    loop = make_loop(tmp_path, executor=ex, state=st)
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.pending_order is not None  # live 未成交
    ex.filled = True  # 下一 tick 订单已成交
    loop.tick(now_ms=1_000_100_000)
    assert loop.state.pending_order is None
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.45
    assert loop.state.position.direction is Direction.UP


def test_pending_not_filled_keeps_pending(tmp_path):
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="oid-1"),
    )
    loop = make_loop(tmp_path, executor=ex, state=st)
    loop.tick(now_ms=1_000_000_000)
    loop.tick(now_ms=1_000_100_000)  # 未成交
    assert loop.state.pending_order is not None
    assert loop.state.position is None


# ---- 止盈/止损 ----

def test_take_profit_sells_position(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,
            window_bet_placed=True,
            position=make_position(),
        ),
        executor=ex,
    )
    ex.best_bid_value = 0.80  # 触发止盈
    loop.tick(now_ms=1_000_100_000)
    assert ("sell", "YES", 2.0) in ex.calls
    assert loop.state.position is None
    trades = Path(tmp_path, "trades.csv").read_text(encoding="utf-8")
    assert "take_profit" in trades
    assert loop.state.consecutive_losses == 0


def test_stop_loss_sells_position(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,
            window_bet_placed=True,
            position=make_position(),
        ),
        executor=ex,
    )
    ex.best_bid_value = 0.30  # 触发止损
    loop.tick(now_ms=1_000_100_000)
    assert loop.state.position is None
    trades = Path(tmp_path, "trades.csv").read_text(encoding="utf-8")
    assert "stop_loss" in trades
    assert loop.state.consecutive_losses == 1


# ---- 结算 ----

def test_settle_win_after_window_end(tmp_path):
    ex = FakeExecutor()
    settled = make_market(price=1.0)  # 结算后 Up=1
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,
            window_bet_placed=True,
            position=make_position(),
        ),
        discovery=FakeDiscovery(None, settled=settled),
        executor=ex,
    )
    loop.tick(now_ms=999_900_000 + WINDOW_MS + 60_000)
    assert loop.state.position is None
    trades = Path(tmp_path, "trades.csv").read_text(encoding="utf-8")
    assert "settle" in trades
    assert loop.state.consecutive_losses == 0


def test_settle_loss_updates_breaker(tmp_path):
    ex = FakeExecutor()
    settled = make_market(price=0.0)  # 结算后 Up=0（跌了）
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,
            window_bet_placed=True,
            position=make_position(),
        ),
        discovery=FakeDiscovery(None, settled=settled),
        executor=ex,
    )
    loop.tick(now_ms=999_900_000 + WINDOW_MS + 60_000)
    assert loop.state.position is None
    assert loop.state.consecutive_losses == 1
    assert loop.state.daily_loss == pytest.approx(2.0 * 0.45)


def test_settle_waits_when_market_not_settled(tmp_path):
    ex = FakeExecutor()
    settled = make_market(price=0.55)  # 未结算（价格在中间）
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,
            window_bet_placed=True,
            position=make_position(),
        ),
        discovery=FakeDiscovery(None, settled=settled),
        executor=ex,
    )
    loop.tick(now_ms=999_900_000 + WINDOW_MS + 60_000)
    assert loop.state.position is not None  # 等待结算


# ---- 熔断 ----

def test_circuit_breaker_pauses(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        state=TradeState(symbol="BTC", window_start=999_900, consecutive_losses=10),
        executor=ex,
    )
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.paused is True
    assert loop.state.pause_reason == "连亏 10 笔（上限 10）"
    assert ex.calls == []


def test_paused_loop_does_not_trade(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        state=TradeState(symbol="BTC", window_start=999_900, paused=True),
        executor=ex,
    )
    loop.tick(now_ms=1_000_000_000)
    assert ex.calls == []


# ---- 窗口切换 ----

def test_next_window_bets_again(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(tmp_path, executor=ex,
                     discovery=FakeDiscovery(make_market(), settled=make_market(price=1.0)))
    loop.tick(now_ms=1_000_000_000)
    assert len(ex.calls) == 1
    loop.tick(now_ms=999_900_000 + WINDOW_MS + 100_000)  # 跨窗口：结算 → 新窗口
    assert loop.state.window_start == 1_000_800  # 下一窗口
    assert sum(1 for c in ex.calls if c[0] == "market_buy") == 2  # 新窗口重新下注


def test_same_window_does_not_rebet(tmp_path):
    ex = FakeExecutor()
    loop = make_loop(tmp_path, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    loop.tick(now_ms=1_000_100_000)  # 同窗口，未成交继续等
    assert len(ex.calls) == 1


# ---- 状态持久化 ----

def test_status_file_written(tmp_path):
    loop = make_loop(tmp_path)
    loop.tick(now_ms=1_000_000_000)
    assert Path(tmp_path, "status.json").is_file()


# ---- code-review 修复项 ----

def test_win_resets_consecutive_loss_streak(tmp_path):
    """盈利平仓后连续亏损计数归零。"""
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,
            window_bet_placed=True,
            consecutive_losses=5,
            position=make_position(),
        ),
        executor=ex,
    )
    ex.best_bid_value = 0.80  # 止盈
    loop.tick(now_ms=1_000_100_000)
    assert loop.state.consecutive_losses == 0


def test_manual_resume_clears_breaker(tmp_path):
    """人工恢复：paused 改回 false 后，熔断计数清零并继续交易。"""
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        state=TradeState(symbol="BTC", window_start=999_900, consecutive_losses=10, paused=False, was_paused=True),
        executor=ex,
    )
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.paused is False  # 未重新熔断
    assert loop.state.consecutive_losses == 0
    assert ex.calls and ex.calls[0][0] == "market_buy"  # 继续交易


def test_daily_loss_pauses(tmp_path):
    from datetime import datetime, timezone

    ex = FakeExecutor()
    tick_day = datetime.fromtimestamp(1_000_000, tz=timezone.utc).strftime("%Y-%m-%d")
    loop = make_loop(
        tmp_path,
        state=TradeState(symbol="BTC", window_start=999_900, daily_loss=10.0, last_day=tick_day),
        executor=ex,
    )
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.paused is True
    assert ex.calls == []


def test_cross_window_pending_cancelled(tmp_path):
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC",
        window_start=999_900,
        window_bet_placed=True,
        pending_order=make_pending(order_id="oid-9"),
    )
    loop = make_loop(tmp_path, state=st, executor=ex)
    loop.tick(now_ms=999_900_000 + WINDOW_MS + 100_000)  # 下一窗口
    assert ("cancel", "oid-9") in ex.calls
    assert loop.state.pending_order is None or loop.state.pending_order.order_id != "oid-9"
    assert sum(1 for c in ex.calls if c[0] == "market_buy") == 1  # 新窗口重新下注


def test_dry_run_fills_when_ask_at_or_below_limit(tmp_path):
    """dry-run 模拟成交：真实盘口 ask ≤ 限价即成交，成交价=盘口价（吃单）。"""
    ex = FakeExecutor()
    ex.best_ask_value = 0.42  # ≤ 限价 0.45
    st = TradeState(
        symbol="BTC",
        window_start=999_900,
        window_bet_placed=True,
        pending_order=make_pending(order_id="dry-run-x"),
    )
    loop = make_loop(tmp_path, state=st, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.pending_order is None
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.42  # 盘口成交价，非限价


def test_dry_run_no_fill_when_ask_above_limit(tmp_path):
    """dry-run 盘口未到价不成交（与真实限价单一致），挂单保留。"""
    ex = FakeExecutor()
    ex.best_ask_value = 0.50  # > 限价 0.45
    st = TradeState(
        symbol="BTC",
        window_start=999_900,
        window_bet_placed=True,
        pending_order=make_pending(order_id="dry-run-x"),
    )
    loop = make_loop(tmp_path, state=st, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.position is None
    assert loop.state.pending_order is not None  # 挂单保留


def test_old_position_take_profit_still_managed(tmp_path):
    """跨窗口旧持仓（结算等待期）：token 盘口结算前有效，止盈/止损照常执行。"""
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,  # 当前窗口（新）
            window_bet_placed=True,
            position=make_position(window=1_000_800),  # 旧窗口持仓
        ),
        executor=ex,
    )
    ex.best_bid_value = 0.80  # ≥ take_profit → 止盈
    loop.tick(now_ms=1_000_000_000)
    assert ("sell", "YES", 2.0) in ex.calls
    assert loop.state.position is None


def test_old_position_stop_loss_still_managed(tmp_path):
    """跨窗口旧持仓同样执行止损（bid 跌破 stop_loss）。"""
    ex = FakeExecutor()
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,
            window_bet_placed=True,
            position=make_position(window=1_000_800),
        ),
        executor=ex,
    )
    ex.best_bid_value = 0.10  # ≤ stop_loss → 止损
    loop.tick(now_ms=1_000_000_000)
    assert ("sell", "YES", 2.0) in ex.calls
    assert loop.state.position is None


def test_settle_timeout_fallback(tmp_path):
    """结算价迟迟不来（价格在中间），超过 1800s 超时后按当前价兑底结算。"""
    ex = FakeExecutor()
    settled = make_market(price=0.55)  # 未结算（中间价）
    loop = make_loop(
        tmp_path,
        state=TradeState(
            symbol="BTC",
            window_start=999_900,
            window_bet_placed=True,
            position=make_position(),
        ),
        discovery=FakeDiscovery(None, settled=settled),
        executor=ex,
    )
    # 窗口结束 + 1900s（超时 1800s）
    loop.tick(now_ms=999_900_000 + WINDOW_MS + 1_900_000)
    assert loop.state.position is None
    trades = Path(tmp_path, "trades.csv").read_text(encoding="utf-8")
    assert "settle" in trades


def test_tick_records_market_prices(tmp_path):
    """每个 tick 把 UP/DOWN 盘口价写入状态（面板展示用）。"""
    ex = FakeExecutor()
    ex.best_bid_value = 0.40
    loop = make_loop(tmp_path, executor=ex,
                     state=TradeState(symbol="BTC", window_start=999_900))
    loop.tick(now_ms=1_000_000_000)
    p = loop.state.market_prices
    assert p is not None
    assert p["up_ask"] == 0.42
    assert p["up_bid"] == 0.40
    assert p["down_ask"] == 0.42
    assert p["down_bid"] == 0.40


def test_market_prices_errors_do_not_break_tick(tmp_path):
    """盘口查询失败不影响 tick（跳过写入，_build_view 不查 bid 故不受影响）。"""
    ex = FakeExecutor()
    ex.best_bid_value = 0.40

    def boom(*a, **k):
        raise RuntimeError("query failed")

    ex.best_bid = boom
    loop = make_loop(tmp_path, executor=ex,
                     state=TradeState(symbol="BTC", window_start=999_900))
    loop.tick(now_ms=1_000_000_000)  # 不抛异常即可
    assert loop.state.market_prices is None  # 查询失败 → 跳过写入


def test_dry_run_fills_immediately_on_place_when_ask_below_limit(tmp_path):
    """挂单时盘口 ask ≤ 限价 → 同一 tick 立即以盘口价成交（限价单吃单行为）。"""
    ex = FakeExecutor()
    ex.best_ask_value = 0.42  # 挂单时刻盘口已低于限价 0.45
    st = TradeState(symbol="BTC", window_start=999_900)
    loop = make_loop(tmp_path, state=st, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.pending_order is None
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.42  # 以实际盘口价成交


def test_market_buy_uses_latest_ask_even_if_volatile(tmp_path):
    """市价买入以 execute 时点的 ask 成交：盘口波动不影响成交，只影响价格。"""
    class VolatileAskExecutor(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.ask_calls = 0

        def best_ask(self, token_id):
            self.ask_calls += 1
            # market_prices(up_ask, down_ask) + _build_view 共 3 次 → 0.42；
            # _execute 市价查询（第 4 次）→ 0.50（盘口已涨回）→ 按 0.50 成交
            return 0.42 if self.ask_calls <= 3 else 0.50

    ex = VolatileAskExecutor()
    st = TradeState(symbol="BTC", window_start=999_900)
    loop = make_loop(tmp_path, state=st, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.50
    assert abs(loop.state.position.size - 1 / 0.50) < 1e-9
    assert loop.state.pending_order is None  # 市价单无挂单


def test_predicting_flag_set_during_signal_generation(tmp_path):
    """推理期间 state.predicting=True 且落盘（面板可实时显示）。"""
    ex = FakeExecutor()
    captured = {}

    def slow_gen(ctx):
        captured["during"] = loop.state.predicting
        captured["start"] = loop.state.predict_start_sec
        return Signal(Direction.UP, 0.70)

    loop = make_loop(tmp_path, executor=ex, strategy=FakeStrategy(signal=None, fn=slow_gen))
    loop.tick(now_ms=1_000_000_000)
    assert captured["during"] is True
    assert captured["start"] == 1_000_000
    assert loop.state.predicting is False  # 完成后复位


def test_lifecycle_phase_transitions(tmp_path):
    """MarketLifecycle 状态机：INIT → RUNNING → DONE（窗口切换）。"""
    from pmbot.market_lifecycle import Phase

    ex = FakeExecutor()
    loop = make_loop(tmp_path, executor=ex)
    loop.tick(now_ms=1_000_000_000)  # 窗口内：INIT(信号) → RUNNING
    assert loop._lifecycle is not None
    assert loop._lifecycle.phase is Phase.RUNNING
    assert loop._lifecycle.window_start == 999_900

    loop.tick(now_ms=1_000_000_000 + 900_000 + 100_000)  # 跨窗口
    assert loop._lifecycle.phase is Phase.RUNNING  # 新窗口 lifecycle
    assert loop._lifecycle.window_start == 1_000_800
    assert loop.state.window_start == 1_000_800


def test_lifecycle_does_not_restart_in_same_window(tmp_path):
    from pmbot.market_lifecycle import Phase

    ex = FakeExecutor()
    loop = make_loop(tmp_path, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    lc1 = loop._lifecycle
    loop.tick(now_ms=1_000_000_000 + 100_000)  # 同窗口
    assert loop._lifecycle is lc1  # 同一生命周期实例
    assert lc1.phase is Phase.RUNNING


def test_shutdown_cancels_pending_and_saves(tmp_path):
    """优雅停机：撤遗留挂单、结算已到期持仓、落盘。"""
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="oid-9"),
    )
    loop = make_loop(tmp_path, executor=ex, state=st)
    loop._lifecycle = None
    loop.shutdown(now_sec=1_000_000)
    assert ("cancel", "oid-9") in ex.calls  # 撤单
    assert Path(tmp_path, "status.json").is_file()  # 落盘


def test_shutdown_settles_expired_position(tmp_path):
    """停机时已到期持仓完成结算（gamma 就绪时）。"""
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        position=Position(Direction.UP, 0.45, 5.0, 0, 999_900),
    )
    loop = make_loop(tmp_path, executor=ex, state=st,
                     discovery=FakeDiscovery(make_market(), settled=make_market(price=1.0)))
    loop.shutdown(now_sec=999_900 + 900 + 10)
    assert st.position is None  # 已结算
    assert Path(tmp_path, "trades.csv").is_file()


def test_shutdown_keeps_unexpired_position(tmp_path):
    """停机时窗口未结束的持仓保留（重启后继续管理）。"""
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        position=Position(Direction.UP, 0.45, 5.0, 800, 999_900),
    )
    loop = make_loop(tmp_path, executor=ex, state=st)
    loop.shutdown(now_sec=1_000_000)
    assert st.position is not None  # 未到期，不结算


class FakeUserStream:
    """可注入的 UserStream 替身：预置事件队列。"""

    def __init__(self, events=None):
        self._events = list(events or [])
        self.markets = []

    def drain(self):
        out, self._events = self._events, []
        return out

    def subscribe_markets(self, conds):
        self.markets = list(conds)


def test_user_event_order_filled_updates_pending(tmp_path):
    """WS order 事件（filled）→ 挂单转持仓。"""
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="oid-ws1"),
    )
    stream = FakeUserStream([("order", {"event_type": "order", "id": "oid-ws1",
                                        "status": "filled"})])
    loop = make_loop(tmp_path, executor=ex, state=st, user_stream=stream)
    loop.tick(now_ms=1_000_000_000)
    assert st.pending_order is None
    assert st.position is not None and st.position.direction == Direction.UP


def test_user_event_order_canceled_clears_pending(tmp_path):
    """WS order 事件（canceled）→ 清挂单。"""
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="oid-ws2"),
    )
    stream = FakeUserStream([("order", {"event_type": "order", "id": "oid-ws2",
                                        "status": "canceled"})])
    loop = make_loop(tmp_path, executor=ex, state=st, user_stream=stream)
    loop.tick(now_ms=1_000_000_000)
    assert st.pending_order is None
    assert st.position is None


def test_user_event_other_order_ignored(tmp_path):
    """非本机挂单的 order 事件不影响状态。"""
    ex = FakeExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="oid-ws3"),
    )
    stream = FakeUserStream([("order", {"event_type": "order", "id": "other-id",
                                        "status": "filled"})])
    loop = make_loop(tmp_path, executor=ex, state=st, user_stream=stream)
    loop.tick(now_ms=1_000_000_000)
    assert st.pending_order is not None
    assert st.position is None


def test_user_stream_subscribed_on_window_switch(tmp_path):
    """窗口切换时 UserStream 订阅当前 condition_id。"""
    ex = FakeExecutor()
    st = TradeState(symbol="BTC", window_start=1_000_000)
    stream = FakeUserStream()
    loop = make_loop(tmp_path, executor=ex, state=st, user_stream=stream)
    loop.tick(now_ms=1_001_000_000)  # 触发市场发现/订阅
    assert stream.markets  # 非空


class CountingExecutor(FakeExecutor):
    """记录 get_order 调用次数的执行器替身。"""

    def __init__(self):
        super().__init__()
        self.get_order_calls = 0

    def get_order(self, order_id):
        self.get_order_calls += 1
        return {"status": "live", "order_id": order_id}


class ConnectedStream(FakeUserStream):
    def __init__(self, events=None):
        super().__init__(events)
        self.connected = True


def test_ws_connected_skips_get_order_polling(tmp_path):
    """WS 连接正常时真实模式不做 get_order 轮询（事件驱动）。"""
    ex = CountingExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="real-oid-1"),
    )
    loop = make_loop(tmp_path, executor=ex, state=st, user_stream=ConnectedStream())
    loop.refresh_pending(make_market(), 1_000_000)
    assert ex.get_order_calls == 0
    assert st.pending_order is not None  # WS 无事件 → 挂单保持


def test_ws_disconnected_falls_back_to_polling(tmp_path):
    """WS 断开时回退 get_order 轮询兜底。"""
    ex = CountingExecutor()
    st = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="real-oid-2"),
    )
    stream = ConnectedStream()
    stream.connected = False
    loop = make_loop(tmp_path, executor=ex, state=st, user_stream=stream)
    loop.refresh_pending(make_market(), 1_000_000)
    assert ex.get_order_calls == 1  # 轮询兜底

    # 无 UserStream（未配置）→ 也走轮询
    ex2 = CountingExecutor()
    st2 = TradeState(
        symbol="BTC", window_start=999_900, window_bet_placed=True,
        pending_order=make_pending(order_id="real-oid-3"),
    )
    loop2 = make_loop(tmp_path, executor=ex2, state=st2)
    loop2.refresh_pending(make_market(), 1_000_000)
    assert ex2.get_order_calls == 1


def test_market_buy_020_holds_1usdc_with_5_shares(tmp_path):
    """用户场景：市价买入，ask=0.2 → 5 股 × 0.2 = 1.0 USDC。"""
    ex = FakeExecutor()
    ex.best_ask_value = 0.20
    loop = make_loop(tmp_path, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    _, token, size = ex.calls[0]
    assert token == "YES"
    assert abs(size - 5) < 1e-9  # 1/0.2 = 5 股
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.20
    assert abs(loop.state.position.size - 5) < 1e-9
    assert abs(loop.state.position.size * loop.state.position.entry_price - 1.0) < 1e-9
