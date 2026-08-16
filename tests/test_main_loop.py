"""主循环测试：窗口流程全链路（注入 fake 策略/发现/执行，dry-run 语义）。

时间约定：窗口 W=999900（秒，15m 对齐）；tick 参数为毫秒。
- 入场 tick: 1_000_000_000（窗口开盘后 100s）
- 下一 tick: 1_000_100_000（+100s）
- 结算 tick: 999_900_000 + 900_000 + 60_000 = 1_000_860_000（窗口结束 60s 后）
- 下一窗口: 999_900_000 + 900_000 + 100_000 = 1_000_900_000 → window=1_000_800
"""

from pathlib import Path

import csv

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
    interval = "15m"  # reset 文件清除用（与真实 MarketDiscovery 同属性）

    def __init__(self, market=None, settled=None):
        self.market = market
        self.settled = settled
        self.invalidated = []

    def find_current_window(self, symbol, now_ms):
        return self.market

    def find_window(self, symbol, window_start, require_tradable=True):
        return self.settled

    def invalidate(self, symbol, window_start, require_tradable=True):
        self.invalidated.append((symbol, window_start, require_tradable))


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.filled = False
        self.best_bid_value = None
        self.best_ask_value = 0.42
        self.sell_proceeds_value = None
        self.live_positions_value = None
        self._sampler = None

    @property
    def sampler(self):
        return self._sampler

    def attach_sampler(self, sampler):
        self._sampler = sampler

    def place_limit(self, token_id, side, price, size):
        self.calls.append(("place", token_id, side, price, size))
        return "oid-1"

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))
        return True

    def market_sell(self, token_id, size):
        self.calls.append(("sell", token_id, size))
        return {"order_id": "sell-oid", "price": self.best_bid_value}

    def sell_proceeds(self, order_id, token_id):
        return self.sell_proceeds_value

    def market_buy(self, token_id, amount):
        self.calls.append(("market_buy", token_id, amount))
        ask = self.best_ask(token_id)
        if ask is None:
            return None
        return {"order_id": "dry-run-mk", "avg_price": ask, "filled_size": amount / ask}

    def best_bid(self, token_id):
        return self.best_bid_value

    def best_ask(self, token_id):
        return self.best_ask_value

    def live_positions(self, user=None):
        return self.live_positions_value

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
              user_stream=None, config=None, dry_run=True):
    st = state or TradeState(symbol="BTC", window_start=None)
    return TradingLoop(
        config=config or CFG,
        symbol="BTC",
        strategy=strategy or FakeStrategy(Signal(Direction.UP, 0.70)),
        discovery=discovery or FakeDiscovery(make_market()),
        executor=executor or FakeExecutor(),
        state=st,
        trades_path=Path(tmp_path) / "trades.csv",
        status_path=Path(tmp_path) / "status.json",
        dry_run=dry_run,
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
    _, token, amount = ex.calls[0]
    assert token == "YES"  # up 方向买 yes
    assert abs(amount - 1.0) < 1e-9  # 市价单金额语义：1 USDC（SDK: BUY=$$$）
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
    _, token, amount = ex.calls[0]
    assert token == "YES"
    assert abs(amount - 1.0) < 1e-9  # 金额 1 USDC，份额由 API 成交返回
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
    _, token, amount = ex.calls[0]
    assert token == "YES"
    assert abs(amount - 1.0) < 1e-9  # 1 USDC 金额
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.20
    assert abs(loop.state.position.size - 5) < 1e-9  # API 成交份额 1/0.2 = 5 股
    assert abs(loop.state.position.size * loop.state.position.entry_price - 1.0) < 1e-9


# ---- 面板控制指令（control.json）----

def _patch_control(tmp_path, monkeypatch, cmd):
    """把主循环的 read_control 指向 tmp 下的指令文件并写入 cmd。"""
    import pmbot.control as control_mod
    from pmbot.main_loop import read_control as _rc

    p = tmp_path / "control.json"
    control_mod.write_control(cmd, p)
    monkeypatch.setattr("pmbot.main_loop.read_control",
                        lambda path: _rc(p))
    return p


def test_control_resume_clears_circuit_breaker(tmp_path, monkeypatch):
    st = TradeState(symbol="BTC", window_start=999_900, paused=True,
                    pause_reason="连亏熔断", consecutive_losses=5, daily_loss=3.0)
    loop = make_loop(tmp_path, state=st)
    _patch_control(tmp_path, monkeypatch, "resume")
    loop.tick(now_ms=1_000_000_000)
    assert st.paused is False
    assert st.pause_reason is None
    assert st.consecutive_losses == 0
    assert st.daily_loss == 0.0
    assert st.was_paused is False


def test_control_reset_without_position_wipes_state(tmp_path, monkeypatch):
    st = TradeState(symbol="BTC", window_start=999_900, daily_loss=4.0,
                    consecutive_losses=2, window_bet_placed=True, last_day="2026-08-14")
    loop = make_loop(tmp_path, state=st)
    trades = tmp_path / "trades.csv"
    trades.write_text("ts\nx\n", encoding="utf-8")
    _patch_control(tmp_path, monkeypatch, "reset")
    loop.tick(now_ms=1_000_000_000)
    assert loop.state is not st, "reset 应重建状态对象"
    assert loop.state.symbol == "BTC"
    assert loop.state.daily_loss == 0.0
    assert loop.state.consecutive_losses == 0
    assert not trades.exists(), "trades.csv 应被删除"


def test_control_reset_with_position_drops_tracking(tmp_path, monkeypatch):
    """有持仓时 reset 也执行：丢弃持仓跟踪（结算自动兑付），防止死锁。"""
    st = TradeState(symbol="BTC", window_start=999_900, daily_loss=4.0,
                    position=make_position(), last_day="2026-08-14")
    loop = make_loop(tmp_path, state=st)
    _patch_control(tmp_path, monkeypatch, "reset")
    loop.tick(now_ms=1_000_000_000)
    assert loop.state is not st, "reset 应重建状态对象"
    assert loop.state.daily_loss == 0.0
    # 旧持仓（entry=0.45）被丢弃；tick 继续执行后按新信号重新入场（entry=ask=0.42）
    assert loop.state.position is not None
    assert loop.state.position.entry_price == 0.42, "旧持仓跟踪应被丢弃，持仓为新入场"


def test_control_stop_sets_shutdown_and_skips_tick(tmp_path, monkeypatch):
    loop = make_loop(tmp_path)
    _patch_control(tmp_path, monkeypatch, "stop")
    loop.tick(now_ms=1_000_000_000)
    assert loop._shutdown is True


def test_control_consumed_file_removed(tmp_path, monkeypatch):
    loop = make_loop(tmp_path)
    p = _patch_control(tmp_path, monkeypatch, "resume")
    loop.tick(now_ms=1_000_000_000)
    assert not p.exists(), "指令消费后文件应被删除"


def test_settle_invalidates_negative_cache(tmp_path):
    """结算市场查询失败时清除负缓存（下 tick 重查），防止永久卡死。"""
    disc = FakeDiscovery(settled=None)  # 市场查询持续失败
    st = TradeState(symbol="BTC", window_start=999_900, position=make_position())
    loop = make_loop(tmp_path, state=st, discovery=disc)
    loop.tick(now_ms=1_000_860_000)  # 窗口结束 60s 后（结算 tick）
    assert disc.invalidated == [("BTC", 999_900, False)], "失败应清除缓存供重查"
    assert st.position is not None, "未超时：持仓保留等待重试"


def test_settle_timeout_drops_stale_position(tmp_path):
    """结算市场持续不可达超过超时 → 丢弃持仓跟踪（Polymarket 结算自动兑付）。"""
    disc = FakeDiscovery(settled=None)
    st = TradeState(symbol="BTC", window_start=999_900,
                    position=make_position(window=997_000))
    loop = make_loop(tmp_path, state=st, discovery=disc)
    loop.tick(now_ms=1_000_000_000)
    assert st.position is None, "超时后应丢弃卡死的持仓跟踪"


def test_no_inference_mid_window_start(tmp_path):
    """窗口中期启动（剩余 ≤ no_entry_before_end_sec）→ 不推理，等窗口切换后才推理。"""
    from dataclasses import replace

    from pmbot.market_lifecycle import Phase
    cfg = replace(CFG, no_entry_before_end_sec=60)
    st = TradeState(symbol="BTC", window_start=999_900)  # 窗口 999900–1000800
    loop = make_loop(tmp_path, state=st, config=cfg)
    # 窗口剩 59s（1000741）：不推理，保持 INIT
    loop.tick(now_ms=1_000_741_000)
    assert st.signal is None, "窗口末不应推理"
    assert loop._lifecycle.phase is Phase.INIT
    # 窗口切换（1000800 新窗口开始）：推理
    loop.tick(now_ms=1_000_800_000)
    assert st.signal is not None, "窗口切换后应推理"
    assert st.signal.direction is Direction.UP


# ---- 钱包余额：快照刷新、今日盈亏基准与真实成交盈亏 ----

class BalanceExecutor(FakeExecutor):
    """带余额查询的 fake：collateral_balance 返回可配置值/异常。"""

    def __init__(self, balance=10.0):
        super().__init__()
        self.balance = balance
        self.queries = 0

    def collateral_balance(self):
        self.queries += 1
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance


def test_balance_refreshes_into_state(tmp_path):
    """余额定时刷新进状态；30s 闸内不重复查询。"""
    ex = BalanceExecutor(balance=12.34)
    loop = make_loop(tmp_path, executor=ex,
                     strategy=FakeStrategy(Signal(Direction.UP, 0.50)))  # 弱信号：无交易干扰
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.balance == 12.34
    q = ex.queries
    loop.tick(now_ms=1_000_000_000 + 5_000)  # <30s：不查
    assert ex.queries == q
    loop.tick(now_ms=1_000_000_000 + 31_000)  # ≥30s：再查
    assert ex.queries == q + 1


def test_balance_query_failure_keeps_old_and_trades(tmp_path):
    """余额查询失败（无凭证/网络）静默保留旧值，不影响入场。"""
    ex = BalanceExecutor(balance=RuntimeError("no creds"))
    loop = make_loop(tmp_path, executor=ex)
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.balance is None
    assert loop.state.position is not None  # 交易不受影响


def test_live_mode_captures_day_start_balance(tmp_path):
    """实盘模式：首次余额刷新捕获今日起始基准（今日盈亏 = 现余额 − 基准）。"""
    ex = BalanceExecutor(balance=20.0)
    loop = make_loop(tmp_path, executor=ex, dry_run=False)
    loop.tick(now_ms=1_000_000_000)
    assert loop.state.day_start_balance == 20.0


def test_day_roll_resets_day_start_balance(tmp_path):
    """跨天重置今日盈亏基准，新基准由下一次余额刷新捕获。

    回归：曾因刷新先于跨天重置执行，把刚捕获的基准清掉（基准恒 None）。
    """
    ex = BalanceExecutor(balance=20.0)
    loop = make_loop(tmp_path, executor=ex, dry_run=False)
    loop.tick(now_ms=1_000_000_000)  # 首日：捕获基准 20.0
    assert loop.state.day_start_balance == 20.0
    # 跨天（次日 0 点后），余额变化：新基准 = 次日首次刷新值
    ex.balance = 25.0
    loop.tick(now_ms=1_000_000_000 + 24 * 3600 * 1000 + 31_000)
    assert loop.state.day_start_balance == 25.0
    assert loop.state.balance == 25.0


# ---- 实时持仓核对（Polymarket /positions 防幽灵持仓） ----

def _eth_pos(size=5.0, outcome="Up"):
    return {"asset": "tok1", "conditionId": "c1", "size": size,
            "avgPrice": 0.5, "curPrice": 0.6, "cashPnl": 0.5,
            "title": "Bitcoin Up or Down - Aug 16", "outcome": outcome}


def test_reconcile_clears_ghost_position(tmp_path):
    """本地有持仓、Polymarket 无本标的持仓（窗口早已结束且超宽限期）→ 幽灵持仓清除。

    注意：买入后立即核对不清除（/positions 索引延迟，见
    test_recent_position_not_cleared_during_index_delay 回归）。
    """
    ex = BalanceExecutor(balance=20.0)
    dis = FakeDiscovery(make_market())
    loop = make_loop(tmp_path, executor=ex, dry_run=False, discovery=dis)
    loop.tick(now_ms=1_000_000_000)  # 入场
    assert loop.state.position is not None
    ex.live_positions_value = []  # Polymarket 确认无任何持仓
    dis.market = None  # 下一 tick 市场不可用：避免窗口切换后重新买入干扰断言
    loop.tick(now_ms=(999_900 + 900 + 180 + 10) * 1000)  # 窗口结束 190s 后（>宽限 180s）
    assert loop.state.position is None  # 幽灵持仓已清除
    assert loop.state.live_positions == []


def test_reconcile_keeps_position_when_polymarket_has_it(tmp_path):
    """Polymarket 有本标的持仓 → 本地持仓保留。"""
    ex = BalanceExecutor(balance=20.0)
    loop = make_loop(tmp_path, executor=ex, dry_run=False)
    loop.tick(now_ms=1_000_000_000)  # 入场
    ex.live_positions_value = [_eth_pos()]
    loop.tick(now_ms=1_000_000_000 + 31_000)
    assert loop.state.position is not None  # 保留
    assert len(loop.state.live_positions) == 1


def test_reconcile_query_failure_keeps_position(tmp_path):
    """查询失败（None）不核对：防误清真实持仓。"""
    ex = BalanceExecutor(balance=20.0)
    loop = make_loop(tmp_path, executor=ex, dry_run=False)
    loop.tick(now_ms=1_000_000_000)  # 入场
    ex.live_positions_value = None  # 查询失败
    loop.tick(now_ms=1_000_000_000 + 31_000)
    assert loop.state.position is not None  # 不动


def test_reconcile_skipped_in_dry_run(tmp_path):
    """dry-run 模拟持仓与真实钱包无关：不核对。"""
    ex = BalanceExecutor(balance=20.0)
    loop = make_loop(tmp_path, executor=ex)  # dry_run=True（默认）
    loop.tick(now_ms=1_000_000_000)  # 入场
    ex.live_positions_value = []
    loop.tick(now_ms=1_000_000_000 + 31_000)
    assert loop.state.position is not None  # 模拟持仓不动


def test_index_delay_does_not_kill_position_and_stop_still_works(tmp_path):
    """事故链回归：买入 → /positions 索引延迟返回空 → 不误清 → 止损仍正常触发。

    曾发生：20:05:30 买入成交，20:05:33 核对返回空被误判幽灵清除 →
    本地 position=null → 止损/时间止损/结算全部失效（资金裸奔、UI 与实盘不符）。
    """
    ex = FakeExecutor()
    loop = make_loop(tmp_path, executor=ex, dry_run=False)
    loop.tick(now_ms=1_000_000_000)  # 入场
    assert loop.state.position is not None
    ex.live_positions_value = []  # 索引延迟：Polymarket 查询暂时为空
    loop.tick(now_ms=1_000_000_031)  # 买入 30s 后核对
    assert loop.state.position is not None  # 不误清（回归：曾在此被清）
    ex.live_positions_value = [_eth_pos()]  # 索引同步：远端出现
    loop.tick(now_ms=1_000_000_061)  # 再核对
    assert loop.state.position is not None  # 保留
    ex.best_bid_value = 0.30  # 触发止损（entry 0.45 → sl 0.36）
    loop.tick(now_ms=1_000_100_000)
    assert loop.state.position is None  # 止损正常平仓
    trades = Path(tmp_path, "trades.csv").read_text(encoding="utf-8")
    assert "stop_loss" in trades


def test_sell_pnl_uses_real_fills(tmp_path):
    """实盘平仓盈亏 = Polymarket 真实成交（卖出收入 − 买入成本），而非余额差值。

    回归：余额差在卖出资金到账前查询 → 止盈也显示 ≈ -成本（如 -1.03 假亏损）。
    """
    ex = BalanceExecutor(balance=20.0)
    loop = make_loop(tmp_path, executor=ex, dry_run=False)
    loop.tick(now_ms=1_000_000_000)  # 入场
    pos = loop.state.position
    # 真实到账（sell_proceeds 聚合）：卖出 0.84，与 best_bid 展示无关
    ex.best_bid_value = 0.9
    ex.sell_proceeds_value = pos.size * 0.84
    loop.tick(now_ms=1_000_000_000 + 60_000)  # 止盈平仓
    assert loop.state.position is None
    expect = pos.size * (0.84 - pos.entry_price)  # 真实成交盈亏（非余额差）
    assert expect > 0
    assert loop.state.consecutive_losses == 0
    import csv

    rows = list(csv.DictReader(open(Path(tmp_path) / "trades.csv", encoding="utf-8")))
    assert rows and abs(float(rows[-1]["pnl"]) - expect) < 1e-5  # CSV 落盘 round(pnl,6)


def test_sell_pnl_falls_back_to_theoretical_when_no_fills(tmp_path):
    """真实成交聚合失败（无订单/网络）时回退理论价差，不产生假亏损。"""
    ex = BalanceExecutor(balance=20.0)
    loop = make_loop(tmp_path, executor=ex, dry_run=False)
    loop.tick(now_ms=1_000_000_000)  # 入场
    pos = loop.state.position
    ex.best_bid_value = 0.9  # sell_proceeds_value=None（聚合失败）
    loop.tick(now_ms=1_000_000_000 + 60_000)  # 止盈平仓
    rows = list(csv.DictReader(open(Path(tmp_path) / "trades.csv", encoding="utf-8")))
    expect = pos.size * (0.9 - pos.entry_price)  # 理论价差（best_bid）
    assert rows and abs(float(rows[-1]["pnl"]) - expect) < 1e-5


def test_dry_run_pnl_uses_theoretical_not_balance_diff(tmp_path):
    """dry-run 不真实下单、钱包余额不变：即使余额可查，盈亏也必须是理论价差。

    回归：曾因余额差恒为 0（exit_balance == entry_balance）把正确的理论
    价差覆盖成 0.00（Web 历史交易显示 +0.00）。
    """
    ex = BalanceExecutor(balance=20.0)  # 余额可查（dry-run 仅面板展示用）
    loop = make_loop(tmp_path, executor=ex)  # dry_run=True（默认）
    loop.tick(now_ms=1_000_000_000)  # 入场
    pos = loop.state.position
    assert pos.entry_price > 0
    ex.best_bid_value = 0.9  # 触发止盈
    loop.tick(now_ms=1_000_000_000 + 60_000)  # 止盈平仓
    assert loop.state.position is None
    import csv

    rows = list(csv.DictReader(open(Path(tmp_path) / "trades.csv", encoding="utf-8")))
    expect = pos.size * (0.9 - pos.entry_price)  # 理论价差（非 0）
    assert expect > 0
    assert rows and abs(float(rows[-1]["pnl"]) - expect) < 1e-5


def test_control_reset_rejected_in_live_mode(tmp_path, monkeypatch):
    """实盘模式拒绝 reset 指令：不丢持仓跟踪、不删交易历史。"""
    st = TradeState(symbol="BTC", window_start=999_900, daily_loss=4.0, last_day="1970-01-12")
    trades = tmp_path / "trades.csv"
    trades.write_text("ts\n2026-01-01T00:00:00+00:00\n", encoding="utf-8")
    loop = make_loop(tmp_path, state=st, dry_run=False)
    _patch_control(tmp_path, monkeypatch, "reset")
    loop.tick(now_ms=1_000_000_000)
    assert st.daily_loss == 4.0  # 状态未清
    assert st.window_start == 999_900
    assert trades.is_file()  # 交易历史未删


def test_start_skips_in_progress_window(tmp_path):
    """启动跳过进行中的窗口：不建生命周期（不推理不交易），下一窗口才运行。"""
    from pmbot.market_lifecycle import Phase

    loop = make_loop(tmp_path)
    loop._skip_window_until = 999_900 + 900  # 模拟 run_forever 启动：跳过当前窗口（step=900s）
    loop.tick(now_ms=1_000_000_000)  # 当前窗口（999900 起点）已进行 100s
    assert loop._lifecycle is None  # 跳过：不推理不交易
    loop.tick(now_ms=1_000_000_000 + 900_000)  # 下一窗口起点（1000800）
    assert loop._lifecycle is not None
    assert loop._lifecycle.window_start == 1_000_800
    assert loop._lifecycle.phase in (Phase.INIT, Phase.RUNNING)


def test_settle_pnl_uses_settle_price(tmp_path):
    """结算（窗口到期自动兑付）盈亏 = 理论价差（结算价 − 入场价）× 股数。

    V2 无手续费：兑付额 = size×1.0，成本 = size×entry_price，理论价差即净盈亏。
    """
    class FeeExecutor(BalanceExecutor):
        def market_buy(self, token_id, amount):
            return {"order_id": "mk-1", "avg_price": 0.57, "filled_size": 1.754384}

    ex = FeeExecutor(balance=8.788843)
    settled = make_market(price=1.0)  # 结算后 Up=1 → 兑付 1.754384
    loop = make_loop(tmp_path, executor=ex, dry_run=False,
                     discovery=FakeDiscovery(make_market(), settled=settled))
    loop.tick(now_ms=1_000_000_000)  # 入场
    pos = loop.state.position
    assert pos.size == 1.754384  # 实际成交股数
    # 窗口结束结算：无卖出订单，走理论价差
    loop.tick(now_ms=1_000_000_000 + WINDOW_MS + 60_000)  # 跨窗口：结算旧持仓 → 新窗口重新入场
    rows = list(csv.DictReader(open(Path(tmp_path) / "trades.csv", encoding="utf-8")))
    assert rows and rows[-1]["reason"] == "settle"
    pnl = float(rows[-1]["pnl"])
    assert pnl == pytest.approx(1.754384 * (1 - 0.57))  # 兑付 − 成本（理论价差）
