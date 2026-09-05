"""MarketLifecycle 直构测试：LifecycleDeps 窄接口注入即可独立验证窗口逻辑。

锚点意义：主循环 92 例测试全部走完整集成路径，LifecycleDeps 协议曾零测试消费
（假接缝）。本文件用 fake deps 直构 MarketLifecycle，验证窗口内编排
（信号生成 → 刷新门控 → 5 接缝顺序 → 收尾撤单）不经 TradingLoop。
"""

from types import SimpleNamespace

from pmbot.market_lifecycle import MarketLifecycle, Phase
from pmbot.types import Action, ActionType, Direction, Signal


class FakeStrategy:
    def __init__(self, refresh: Signal | None = None):
        self._refresh = refresh
        self.generated = 0
        self.refreshed = 0

    def generate_signal(self, ctx=None):
        self.generated += 1
        return Signal(direction=Direction.UP, p_up=0.9)

    def refresh_signal(self, ctx=None):
        self.refreshed += 1
        return self._refresh


def make_deps(**over) -> SimpleNamespace:
    calls: list[str] = []
    state = SimpleNamespace(
        signal=None, position=None, window_bet_placed=False,
        pending_order=None, predicting=False,
        predict_start_sec=None, last_predict_sec=None,
    )
    deps = SimpleNamespace(
        state=state,
        strategy=FakeStrategy(),
        config=SimpleNamespace(no_entry_before_end_sec=0),
        step_sec=900,
        executor=SimpleNamespace(cancel=lambda oid: calls.append(f"cancel:{oid}")),
        refresh_pending=lambda market, now: calls.append("refresh_pending"),
        build_view=lambda market, now: calls.append("build_view") or SimpleNamespace(),
        decide=lambda view: (calls.append("decide"), Action(ActionType.SKIP))[1],
        execute=lambda action, market, now: calls.append("execute"),
        save_status=lambda: calls.append("save_status"),
        calls=calls,
    )
    for k, v in over.items():
        setattr(deps, k, v)
    return deps


def new_lifecycle(deps, window_start=10000, now_sec=10):
    return MarketLifecycle(deps=deps, window_start=window_start, now_sec=now_sec)


# ---- INIT → RUNNING ----

def test_start_generates_signal_and_enters_running():
    deps = make_deps()
    lc = new_lifecycle(deps, now_sec=10)
    assert lc.phase is Phase.INIT
    lc.start(now_sec=10, market=SimpleNamespace())
    assert deps.strategy.generated == 1
    assert deps.state.signal is not None and deps.state.signal.direction is Direction.UP
    assert lc.phase is Phase.RUNNING


def test_start_skips_reasoning_when_signal_exists():
    deps = make_deps()
    deps.state.signal = Signal(direction=Direction.UP, p_up=0.9)
    lc = new_lifecycle(deps)
    lc.start(now_sec=10, market=SimpleNamespace())
    assert deps.strategy.generated == 0  # 已有信号不再重复推理
    assert lc.phase is Phase.RUNNING


def test_start_blocks_when_window_ending():
    deps = make_deps()
    deps.config = SimpleNamespace(no_entry_before_end_sec=60)
    lc = new_lifecycle(deps, window_start=10000, now_sec=10850)  # 剩余 50s ≤ 60 禁入
    lc.start(now_sec=10850, market=SimpleNamespace())
    assert deps.strategy.generated == 0
    assert lc.phase is Phase.INIT  # 窗口末禁入：保持 INIT 等窗口切换


# ---- RUNNING 编排 ----

def test_tick_invokes_five_seams_in_order():
    deps = make_deps()
    lc = new_lifecycle(deps)
    lc.start(now_sec=10, market=SimpleNamespace())
    deps.calls.clear()
    lc.tick(now_sec=11, market=SimpleNamespace())
    assert deps.calls == ["refresh_pending", "build_view", "decide", "execute", "save_status"]


def test_tick_noop_outside_running():
    deps = make_deps()
    lc = new_lifecycle(deps, now_sec=10)  # 未 start：phase INIT
    lc.tick(now_sec=11, market=SimpleNamespace())
    assert deps.calls == []


def test_refresh_signal_updates_direction():
    deps = make_deps()
    deps.state.signal = Signal(direction=Direction.UP, p_up=0.9)
    deps.strategy = FakeStrategy(refresh=Signal(direction=Direction.DOWN, p_up=0.1))
    deps.state.window_bet_placed = False
    lc = new_lifecycle(deps)
    lc.start(now_sec=10, market=SimpleNamespace())  # signal 已有 → 不生成
    lc.tick(now_sec=11, market=SimpleNamespace())
    assert deps.state.signal.direction is Direction.DOWN  # 刷新生效


def test_refresh_gated_when_position_held():
    deps = make_deps()
    deps.state.signal = Signal(direction=Direction.UP, p_up=0.9)
    deps.state.position = SimpleNamespace(direction="up")
    deps.state.window_bet_placed = False
    lc = new_lifecycle(deps)
    lc.start(now_sec=10, market=SimpleNamespace())
    lc.tick(now_sec=11, market=SimpleNamespace())
    assert deps.strategy.refreshed == 0  # 有持仓：不刷新入场意图


def test_refresh_gated_when_bet_placed():
    deps = make_deps()
    deps.state.signal = Signal(direction=Direction.UP, p_up=0.9)
    deps.state.window_bet_placed = True
    lc = new_lifecycle(deps)
    lc.start(now_sec=10, market=SimpleNamespace())
    lc.tick(now_sec=11, market=SimpleNamespace())
    assert deps.strategy.refreshed == 0  # 已下注：不刷新


# ---- 收尾 ----

def test_stop_cancels_pending_and_sets_done():
    deps = make_deps()
    deps.state.pending_order = SimpleNamespace(order_id="ord-1")
    lc = new_lifecycle(deps)
    lc.start(now_sec=10, market=SimpleNamespace())
    deps.calls.clear()
    lc.stop(now_sec=20)
    assert deps.calls == ["cancel:ord-1"]
    assert lc.phase is Phase.DONE


def test_stop_idempotent():
    deps = make_deps()
    deps.state.pending_order = SimpleNamespace(order_id="ord-1")
    lc = new_lifecycle(deps)
    lc.start(now_sec=10, market=SimpleNamespace())
    deps.calls.clear()
    lc.stop(now_sec=20)
    lc.stop(now_sec=21)  # 第二次：DONE 提前返回
    assert deps.calls == ["cancel:ord-1"]