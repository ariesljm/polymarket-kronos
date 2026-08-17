"""backtest_sim 纯逻辑测试：路径模拟、手续费、窗口成交拉取。"""

import pytest

from pmbot import backtest_sim as bs


def test_simulate_path_take_profit():
    """价格触达止盈 → take_profit 退出。"""
    # entry=0.5, TP_PCT=0.30 → tp=0.65；SL=0.40
    path = [(0, 0.50), (100, 0.60), (200, 0.66)]
    r = bs.simulate_path(path, entry_price=0.5, size=5, is_taker=False)
    assert r["reason"] == "take_profit"
    assert r["exit"] == pytest.approx(0.65)


def test_simulate_path_stop_loss():
    """价格触达止损 → stop_loss 退出。"""
    path = [(0, 0.50), (100, 0.42), (200, 0.38)]
    r = bs.simulate_path(path, entry_price=0.5, size=5, is_taker=False)
    assert r["reason"] == "stop_loss"
    assert r["exit"] == pytest.approx(0.40)


def test_simulate_path_window_end():
    """全程未触发 → 窗口结束以最后价退出。"""
    path = [(0, 0.50), (100, 0.55), (299, 0.58)]
    r = bs.simulate_path(path, entry_price=0.5, size=5, is_taker=False)
    assert r["reason"] == "window_end"
    assert r["exit"] == pytest.approx(0.58)


def test_trade_fee_formula():
    """taker 手续费 = size × 费率 × entry × (1-entry)。"""
    r = bs._trade(entry=0.5, exit_p=0.6, size=5, is_taker=True, reason="x", hold_s=10)
    assert r["fee"] == pytest.approx(5 * bs.TAKER_FEE_RATE * 0.5 * 0.5)
    assert r["pnl"] == pytest.approx((0.6 - 0.5) * 5 - r["fee"])


def test_trade_maker_no_fee():
    r = bs._trade(entry=0.5, exit_p=0.6, size=5, is_taker=False, reason="x", hold_s=10)
    assert r["fee"] == 0.0


def test_fetch_window_filters_and_aggregates(monkeypatch):
    """fetch_window: 按 yes token + 窗口时间过滤，秒级均价。"""
    rows = [
        {"asset": "yes-tok", "timestamp": 100, "price": 0.50},
        {"asset": "yes-tok", "timestamp": 100, "price": 0.52},
        {"asset": "no-tok", "timestamp": 100, "price": 0.50},   # 其他 token 忽略
        {"asset": "yes-tok", "timestamp": 9999, "price": 0.99},  # 窗口外忽略
        {"asset": "yes-tok", "timestamp": 200, "price": 0.60},
    ]
    state = {"n": 0}

    def fake_get(url, params, proxies=None, timeout=30):
        state["n"] += 1
        return type("R", (), {"raise_for_status": lambda self: None,
                              "json": lambda self: rows if state["n"] == 1 else []})()

    monkeypatch.setattr(bs.requests, "get", fake_get)
    path = bs.fetch_window("cid", ["yes-tok", "no-tok"], win_start=0, win_end=300)
    assert path == [(100, 0.51), (200, 0.60)]  # 秒级均价 0.50/0.52 → 0.51
    assert state["n"] == 1  # 不足 1000 条提前 break，不分页
