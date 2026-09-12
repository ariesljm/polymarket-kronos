"""入场价闸门测试：有效区间解析 + 上下限判定（engine 与执行层共用单一事实源）。"""

from pmbot.engine import AutoTuneOverride
from pmbot.entry_gate import EntryGate, resolve


def test_resolve_without_override_uses_config():
    g = resolve(0.30, 0.60, None)
    assert (g.min_price, g.max_price) == (0.30, 0.60)


def test_resolve_override_replaces_max_only():
    g = resolve(0.30, 0.60, AutoTuneOverride(max_entry_price=0.50, take_profit=0.95))
    assert g.min_price == 0.30  # 下限无自适应
    assert g.max_price == 0.50


def test_resolve_empty_delta_keeps_config_max():
    """delta 空（字段全 None）→ 上限回落 config。"""
    assert resolve(0.30, 0.60, AutoTuneOverride()).max_price == 0.60


def test_check_above_max_is_cap():
    assert EntryGate(max_price=0.60).check(0.61) == "entry_price_cap"


def test_check_below_min_is_floor():
    assert EntryGate(min_price=0.30).check(0.29) == "entry_price_floor"


def test_check_boundaries_pass():
    g = EntryGate(min_price=0.30, max_price=0.60)
    assert g.check(0.30) is None  # 边界含
    assert g.check(0.60) is None


def test_check_zero_disables_side():
    g = EntryGate()  # 上下限均不限制（None）
    assert g.check(0.99) is None
    assert g.check(0.01) is None


def test_resolve_config_zero_is_no_limit():
    """config 的 `0 = 关闭` 映射为 None（不限制），非「上限 0」。"""
    g = resolve(0.0, 0.0, None)
    assert (g.min_price, g.max_price) == (None, None)
    assert g.check(0.99) is None
    assert g.check(0.01) is None


def test_resolve_override_zero_cap_rejects_all():
    """auto_tune 把上限收窄到 0（最低价带累计 EV≤0）→ 拒绝一切，不得回落 config。

    回归（2026-09-12 实盘事故）：0.0 曾被 `max_price > 0` 当成「不限制」→ 盘口
    0.98 静默放行并成交；0.0 与 None 语义必须分开。
    """
    g = resolve(0.30, 0.60, AutoTuneOverride(max_entry_price=0.0))
    assert g.max_price == 0.0
    assert g.check(0.98) == "entry_price_cap"
    assert g.check(0.05) == "entry_price_cap"  # 拒绝一切
    assert g.min_price == 0.30  # 下限不受影响


def test_check_missing_ask_passes():
    assert EntryGate(min_price=0.30, max_price=0.60).check(None) is None
