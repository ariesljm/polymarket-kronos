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
    g = EntryGate()  # 上下限均关闭
    assert g.check(0.99) is None
    assert g.check(0.01) is None


def test_check_missing_ask_passes():
    assert EntryGate(min_price=0.30, max_price=0.60).check(None) is None
