"""退出规则纯函数测试：止盈/止损价计算边界。"""

import pytest

from pmbot.exit_rules import position_exit_levels


def test_basic_percentage():
    tp, sl = position_exit_levels(0.5, tp_pct=0.30, sl_pct=0.20)
    assert tp == pytest.approx(0.65)
    assert sl == pytest.approx(0.40)


def test_tp_capped_at_max():
    """止盈封顶：+100% 超过 tp_max → 取 tp_max。"""
    tp, _ = position_exit_levels(0.9, tp_pct=1.0, sl_pct=0.2, tp_max=0.95)
    assert tp == 0.95


def test_sl_disabled_when_zero():
    """sl_pct=0 关闭止损 → 止损价 0.0。"""
    tp, sl = position_exit_levels(0.5, tp_pct=0.3, sl_pct=0.0)
    assert sl == 0.0


def test_sl_floor_protection():
    """极端止损百分比 → 价格被 floor 保护（不为 0/负）。"""
    _, sl = position_exit_levels(0.01, tp_pct=0.1, sl_pct=0.99, floor=0.001)
    assert sl == 0.001


def test_engine_semantics_preserved():
    """engine 现语义（tp_max=0.95 config 值）保持。"""
    tp, sl = position_exit_levels(0.28, tp_pct=0.80, sl_pct=0.10, tp_max=0.95)
    assert tp == pytest.approx(0.504)
    assert sl == pytest.approx(0.252)
