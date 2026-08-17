"""Settler 结算状态机单元测试：注入窄替身直测规则，不构造 TradingLoop。

对应候选 1（结算状态机深模块化）：12+ 结算规则此前只能走完整 tick 集成
（FakeDiscovery/FakeExecutor/TradeState 三件套），现在窄接口 + 回调直测。
"""

import pytest

from pmbot.settler import Settler, SettlePhase
from pmbot.types import Direction, Position


class FakeSource:
    """结算市场查询窄替身：可设不可达 / 结算价。"""

    def __init__(self, price=None, reachable=True):
        self.price = price
        self.reachable = reachable
        self.invalidated = []

    def find_window(self, symbol, window_start, require_tradable=True):
        if not self.reachable:
            return None
        from pmbot.market_discovery import MarketInfo

        return MarketInfo(
            condition_id="c1", yes_token_id="YES", no_token_id="NO",
            outcome_prices=(self.price, 1 - self.price),
            accepting_orders=True, question="t", end_date="",
            window_start=window_start,
        )

    def invalidate(self, symbol, window_start, require_tradable=True):
        self.invalidated.append(window_start)


def make_pos(direction=Direction.UP, entry=0.45, size=2.0, window_start=999_900):
    return Position(
        direction=direction, entry_price=entry, size=size,
        entered_remaining_sec=300, window_start=window_start,
    )


def make_settler(source, *, dry_run=True, timeout=None, step=300,
                 proceeds_fn=None):
    calls = {"settle": [], "abandon": 0}

    def on_settle(pos, exit_price, proceeds):
        calls["settle"].append((pos, exit_price, proceeds))

    def on_abandon():
        calls["abandon"] += 1

    s = Settler(
        symbol="BTC",
        source=source,
        settle_proceeds=proceeds_fn or (lambda cid: None),
        on_settle=on_settle,
        on_abandon=on_abandon,
        step_sec=step,
        settle_timeout_sec=timeout,
        dry_run=dry_run,
    )
    return s, calls


def test_should_run_window_ended():
    """should_run：持仓窗口已结束才进入结算流程（窗口 = start + step）。"""
    s, _ = make_settler(FakeSource())
    pos = make_pos(window_start=999_900)
    assert not s.should_run(999_899, pos)      # 窗口未结束
    assert not s.should_run(1_000_199, pos)    # 窗口结束前一秒
    assert s.should_run(1_000_200, pos)        # 窗口结束瞬间
    assert not s.should_run(1_000_200, None)   # 无持仓


def test_settle_market_unreachable_waits_then_abandons():
    """市场不可达：invalidate 防负缓存 → 等；超时后丢弃持仓跟踪。"""
    src = FakeSource(reachable=False)
    s, calls = make_settler(src, timeout=600)
    pos = make_pos()
    s.settle(1_000_000, pos)  # 窗口结束 100s（未超时）
    assert src.invalidated == [999_900]
    assert s.phase is SettlePhase.PRICE_WAIT
    assert calls["settle"] == [] and calls["abandon"] == 0
    s.settle(1_001_000, pos)  # 超过 timeout_at（999900+300+600）
    assert calls["abandon"] == 1
    assert s.phase is SettlePhase.DONE


def test_settle_loss_zero_price_clears_immediately():
    """结算归零（输）：立即按成本记账，不查 REDEEM（无兑付记录）。"""
    src = FakeSource(price=0.0)
    s, calls = make_settler(src, dry_run=False)  # 实盘：也不应查兑付
    s.settle(1_000_000, make_pos())
    assert len(calls["settle"]) == 1
    pos, exit_price, proceeds = calls["settle"][0]
    assert exit_price == 0.0
    assert proceeds is None
    assert s.phase is SettlePhase.DONE


def test_settle_loss_near_zero_residual_price():
    """结算残值（0.005）按归零处理：清仓且残值计入盈亏。"""
    src = FakeSource(price=0.005)
    s, calls = make_settler(src, dry_run=False)
    s.settle(1_000_000, make_pos())
    assert len(calls["settle"]) == 1
    assert calls["settle"][0][1] == pytest.approx(0.005)
    assert calls["settle"][0][2] is None


def test_settle_mid_price_waits_then_forces_close():
    """中间价（未结算）：等待；超时后按当前价兜底记账。"""
    src = FakeSource(price=0.55)
    s, calls = make_settler(src, timeout=600)
    pos = make_pos()
    s.settle(1_000_000, pos)  # 未超时
    assert s.phase is SettlePhase.PRICE_WAIT
    assert calls["settle"] == []
    s.settle(1_001_000, pos)  # 超时（999900+300+600）
    assert len(calls["settle"]) == 1
    assert calls["settle"][0][1] == pytest.approx(0.55)
    assert s.phase is SettlePhase.DONE


def test_settle_win_waits_redeem_then_settles():
    """赢：结算价就绪 → 等 REDEEM 确认；确认后按真实兑付记账。"""
    proceeds = {"v": None}
    src = FakeSource(price=1.0)

    def pf(cid):
        return proceeds["v"]

    s, calls = make_settler(src, dry_run=False, proceeds_fn=pf)
    pos = make_pos()
    s.settle(1_000_000, pos)  # REDEEM 未出现
    assert s.phase is SettlePhase.REDEEM_WAIT
    assert calls["settle"] == []
    proceeds["v"] = 1.9  # 兑付到账
    s.settle(1_000_010, pos)
    assert len(calls["settle"]) == 1
    pos2, exit_price, p = calls["settle"][0]
    assert exit_price == 1.0 and p == 1.9
    assert s.phase is SettlePhase.DONE


def test_settle_win_dry_run_settles_immediately():
    """dry-run 赢：不查兑付，直接按结算价记账。"""
    src = FakeSource(price=1.0)
    s, calls = make_settler(src, dry_run=True)
    s.settle(1_000_000, make_pos())
    assert len(calls["settle"]) == 1
    assert calls["settle"][0][2] is None  # 无 proceeds


def test_settle_direction_picks_correct_outcome():
    """方向选择结算价：UP 用 outcome_prices[0]，DOWN 用 [1]。"""
    src = FakeSource(price=0.0)  # (up=0.0, down=1.0) → UP 输、DOWN 赢
    s, calls = make_settler(src, dry_run=True)
    s.settle(1_000_000, make_pos(direction=Direction.DOWN))
    assert len(calls["settle"]) == 1
    assert calls["settle"][0][1] == pytest.approx(1.0)  # DOWN 结算价 1


def test_default_timeout_scales_with_step():
    """默认超时：2×窗口步长，下限 300s。"""
    assert Settler.default_timeout_sec(300) == 600
    assert Settler.default_timeout_sec(900) == 1800
    assert Settler.default_timeout_sec(60) == 300  # 下限
