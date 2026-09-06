"""auto_tune 自适应调参测试：门槛保护 / 收窄 max_entry / 低带胜率调止盈。

正常路径（真实交易）:
- 样本不足(全局 < min_n) → 空覆盖(不动参数)
- 某价格带样本足且累计 EV ≤ 0 → max_entry 收窄到该带下界
- 低价带(≤0.45)胜率显著 → take_profit 上调
- 全部正 EV → 维持 config(只收窄不放宽)
"""

from __future__ import annotations

import pytest

from pmbot.auto_tune import BandStat, band_stats, tune, tune_reason
from pmbot.engine import AutoTuneOverride


class _T:
    """最小 trade 替身（仅暴露 auto_tune 需要的字段）。"""

    def __init__(self, entry_price: float, pnl: float):
        self.entry_price = entry_price
        self.pnl = pnl


def _trades(rows: list[tuple]) -> list:
    """rows: [(entry_price, pnl), ...]"""
    return [_T(p, pnl) for p, pnl in rows]


def test_insufficient_samples_no_change():
    """全局样本不足 → 空 override（不基于噪声调参）。"""
    out = tune(_trades([(0.30, 0.2), (0.50, -1.0), (0.31, 0.1)]), min_band_n=10)
    assert out == AutoTuneOverride()
    assert out.max_entry_price is None
    assert out.take_profit is None


def test_high_band_negative_narrows_max_entry():
    """0.8+ 带样本足且亏损 → max_entry 收窄到 0.8（低于 config 时生效）。"""
    rows = (
        [(0.20, 0.3) for _ in range(10)]            # 0-0.3 正
        + [(0.40, 0.2) for _ in range(10)]          # 0.3-0.5 正
        + [(0.83, -0.9) for _ in range(10)]         # 0.8-0.95 负,样本足
    )
    out = tune(_trades(rows), min_band_n=10, config_max_entry=0.9)
    assert out.max_entry_price is not None
    assert out.max_entry_price == 0.8  # 收窄到负带的边界


def test_narrow_never_above_config():
    """收窄结果不高于 config（只收窄不放宽）。"""
    rows = [(0.83, -0.9) for _ in range(10)]
    out = tune(_trades(rows), min_band_n=10, config_max_entry=0.65)
    assert out.max_entry_price == 0.65  # 0.8 收缴但 config 更严 → 维持 config
    """所有带正 EV → override 不出现（纯 config 运行）。"""
    rows = [(p, 0.2) for p in (0.1, 0.3, 0.5, 0.7) for _ in range(12)]
    out = tune(_trades(rows), min_band_n=10, config_max_entry=0.65)
    assert out.max_entry_price is None


def test_low_band_winrate_raises_take_profit():
    """低价带胜率 70% 且样本足 → take_profit 上调（接近持有）。"""
    rows = [(p, 0.3) for p in (0.15, 0.25, 0.35, 0.40) for _ in range(10)]
    rows += [(0.20, -1.0)] * 3  # 少量亏损,胜率仍高
    out = tune(_trades(rows), min_band_n=10, config_max_entry=0.65, config_take_profit=0.95)
    assert out.take_profit is not None
    assert out.take_profit == 0.99


def test_band_stats_aggregation():
    stats = band_stats(_trades([(0.1, 0.5), (0.2, -0.3), (0.7, 0.4)]))
    b03 = stats[(0.0, 0.3)]
    assert b03 == BandStat(n=2, pnl=0.2, wins=1)
    assert stats[(0.65, 0.8)] == BandStat(n=1, pnl=0.4, wins=1)
    assert b03.ev == pytest.approx(0.1)


def test_tune_reason_text():
    stats = band_stats(_trades([(0.1, 0.2)]))
    reason = tune_reason(AutoTuneOverride(), stats)
    assert "无调整" in reason