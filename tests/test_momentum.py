"""momentum 策略测试：分标的阈值 + BTC 反向矛盾过滤。"""

from pmbot.config import StrategyConfig
from pmbot.strategies.momentum import MomentumStrategy
from pmbot.types import Direction


def make_strategy(
    *,
    symbol="ETH",
    threshold_pct=0.08,
    threshold_by_symbol=None,
    btc_contradiction_pct=0.0,
    fetch_price=None,
    fetch_window_open=None,
    fetch_btc_price=None,
):
    sc = StrategyConfig(
        threshold_pct=threshold_pct,
        threshold_by_symbol=threshold_by_symbol or {},
        btc_contradiction_pct=btc_contradiction_pct,
    )
    return MomentumStrategy(
        strategy_config=sc,
        symbol=symbol,
        fetch_price=fetch_price or (lambda: None),
        fetch_window_open=fetch_window_open or (lambda: 100.0),
        fetch_btc_price=fetch_btc_price or (lambda: None),
    )


def test_threshold_by_symbol_override():
    """分标的阈值覆盖默认值：SOL 用 0.12，ETH 用 0.10。"""
    sol = make_strategy(symbol="SOL", threshold_by_symbol={"ETH": 0.10, "SOL": 0.12})
    assert sol.threshold_pct == 0.12
    eth = make_strategy(symbol="ETH", threshold_by_symbol={"ETH": 0.10, "SOL": 0.12})
    assert eth.threshold_pct == 0.10


def test_threshold_default_when_symbol_not_listed():
    """未列出的标的回退默认 threshold_pct。"""
    btc = make_strategy(symbol="BTC", threshold_pct=0.08, threshold_by_symbol={"ETH": 0.10})
    assert btc.threshold_pct == 0.08


def test_btc_contradiction_filters_up_signal():
    """标的涨穿但 BTC 反向跌穿 → 降级 SKIP。"""
    s = make_strategy(
        symbol="ETH",
        btc_contradiction_pct=0.08,
        fetch_price=lambda: 100.10,       # 本标的 +0.1% 涨穿
        fetch_window_open=lambda: 100.0,
        fetch_btc_price=lambda: 99.90,    # BTC -0.1% 跌穿（反向）
    )
    s._btc_base = 100.0  # 绕过 BTC 基准懒加载（避免真实网络）
    sig = s.generate_signal()
    assert sig.direction is Direction.SKIP


def test_btc_contradiction_filters_down_signal():
    """标的跌穿但 BTC 反向涨穿 → 降级 SKIP。"""
    s = make_strategy(
        symbol="ETH",
        btc_contradiction_pct=0.08,
        fetch_price=lambda: 99.90,        # 本标的 -0.1% 跌穿
        fetch_window_open=lambda: 100.0,
        fetch_btc_price=lambda: 100.10,   # BTC +0.1% 涨穿（反向）
    )
    s._btc_base = 100.0
    sig = s.generate_signal()
    assert sig.direction is Direction.SKIP


def test_btc_contradiction_allows_aligned():
    """标的与 BTC 同向 → 不过滤，返回穿越信号。"""
    s = make_strategy(
        symbol="ETH",
        btc_contradiction_pct=0.08,
        fetch_price=lambda: 100.10,       # 本标的 +0.1%
        fetch_window_open=lambda: 100.0,
        fetch_btc_price=lambda: 100.05,   # BTC +0.05%（未反向跌穿）
    )
    s._btc_base = 100.0
    sig = s.generate_signal()
    assert sig.direction is Direction.UP


def test_btc_filter_disabled_when_zero():
    """btc_contradiction_pct=0 → 不过滤（即使 BTC 反向）。"""
    s = make_strategy(
        symbol="ETH",
        btc_contradiction_pct=0.0,
        fetch_price=lambda: 100.10,
        fetch_window_open=lambda: 100.0,
        fetch_btc_price=lambda: 99.90,
    )
    sig = s.generate_signal()
    assert sig.direction is Direction.UP


def test_btc_filter_skipped_when_btc_price_missing():
    """BTC 价缺失 → 不误杀（宁缺毋滥，返回原信号）。"""
    s = make_strategy(
        symbol="ETH",
        btc_contradiction_pct=0.08,
        fetch_price=lambda: 100.10,
        fetch_window_open=lambda: 100.0,
        fetch_btc_price=lambda: None,
    )
    s._btc_base = 100.0
    sig = s.generate_signal()
    assert sig.direction is Direction.UP
