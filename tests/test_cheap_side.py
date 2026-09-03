"""cheap_side 策略测试:比价选方向、门槛过滤、无盘口兜底。"""

from pmbot.config import StrategyConfig
from pmbot.strategies.cheap_side import CheapSideStrategy
from pmbot.types import Direction


def mk(ask_fn, threshold: float = 0.35):
    sc = StrategyConfig(entry_price_threshold=threshold)
    return CheapSideStrategy(strategy_config=sc, best_ask_fn=ask_fn)


def test_picks_lower_ask_within_threshold():
    """down ask 更低且 ≤ 门槛 → 买 down（强信号 p_up=0.1 供 engine 阈值链路）。"""
    st = mk(lambda d: {"up": 0.30, "down": 0.22}[d.value])
    sig = st.generate_signal({"now_ms": 0})
    assert sig.direction is Direction.DOWN
    assert sig.p_up == 0.10


def test_picks_up_when_up_cheaper():
    st = mk(lambda d: {"up": 0.18, "down": 0.40}[d.value])
    sig = st.generate_signal({"now_ms": 0})
    assert sig.direction is Direction.UP
    assert sig.p_up == 0.90


def test_skips_when_cheapest_above_threshold():
    """两侧都超门槛 → 无廉价方向，SKIP（不入场）。"""
    st = mk(lambda d: {"up": 0.44, "down": 0.40}[d.value])
    sig = st.generate_signal({"now_ms": 0})
    assert sig.direction is Direction.SKIP


def test_threshold_boundary_inclusive():
    st = mk(lambda d: {"up": 0.35, "down": 0.48}[d.value], threshold=0.35)
    sig = st.generate_signal({"now_ms": 0})
    assert sig.direction is Direction.UP  # ask == 门槛 允许入场
    st = mk(lambda d: {"up": 0.36, "down": 0.48}[d.value], threshold=0.35)
    assert st.generate_signal({"now_ms": 0}).direction is Direction.SKIP


def test_one_side_missing_uses_available():
    """单边缺失(up 无报价)：用可用方向，仍受门槛约束。"""
    st = mk(lambda d: {"up": None, "down": 0.25}[d.value])
    assert st.generate_signal({"now_ms": 0}).direction is Direction.DOWN
    st = mk(lambda d: {"up": None, "down": 0.60}[d.value])
    assert st.generate_signal({"now_ms": 0}).direction is Direction.SKIP


def test_no_ask_fn_skips():
    """无盘口查询能力 → SKIP（不臆测方向）。"""
    st = CheapSideStrategy(strategy_config=StrategyConfig())
    assert st.generate_signal({"now_ms": 0}).direction is Direction.SKIP


def test_context_injected_ask_fn_used():
    """运行态经信号上下文注入 ask（生命周期注入路径）。"""
    st = CheapSideStrategy(strategy_config=StrategyConfig())  # 无构造注入
    sig = st.generate_signal({"now_ms": 0, "best_ask": lambda d: {"up": 0.31, "down": 0.5}[d.value]})
    assert sig.direction is Direction.UP