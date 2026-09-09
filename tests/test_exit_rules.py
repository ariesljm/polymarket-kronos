"""退出规则纯函数测试：止盈/止损价计算边界。"""

import pytest

from pmbot.exit_rules import position_exit_levels


def test_tp_is_absolute():
    """止盈为绝对价：tp_price 直接作为止盈价。"""
    tp, sl = position_exit_levels(0.5, tp_price=0.95, sl_pct=0.20)
    assert tp == pytest.approx(0.95)
    assert sl == pytest.approx(0.40)


def test_tp_absolute_independent_of_entry():
    """绝对止盈价与入场价无关：低带持仓也持有到 0.95（接近结算）。"""
    tp, _ = position_exit_levels(0.27, tp_price=0.95, sl_pct=0.0)
    assert tp == 0.95


def test_sl_disabled_when_zero():
    """sl_pct=0 关闭止损 → 止损价 0.0。"""
    tp, sl = position_exit_levels(0.5, tp_price=0.95, sl_pct=0.0)
    assert sl == 0.0


def test_sl_floor_protection():
    """极端止损百分比 → 价格被 floor 保护（不为 0/负）。"""
    _, sl = position_exit_levels(0.01, tp_price=0.95, sl_pct=0.99, floor=0.001)
    assert sl == 0.001
