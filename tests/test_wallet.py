"""WalletReconciler 单元测试：余额快照/今日盈亏基准/幽灵持仓核对（直测，无需完整引擎）。"""

from pmbot.state import TradeState
from pmbot.types import Direction, Position
from pmbot.wallet import WalletReconciler


class FakeWallet:
    """WalletSource 窄替身：余额/实时持仓可配置值与查询计数。"""

    def __init__(self, balance=10.0, positions=None):
        self.balance = balance
        self.positions = positions
        self.balance_queries = 0
        self.position_queries = 0

    def collateral_balance(self):
        self.balance_queries += 1
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance

    def live_positions(self, user=None):
        self.position_queries += 1
        return self.positions


def make_state(**kw) -> TradeState:
    return TradeState(symbol="BTC", **kw)


def make_pos(**kw) -> Position:
    return Position(Direction.UP, 0.45, 2.0, 800, 999_900, **kw)


def _eth_pos(size=5.0):
    return {"asset": "tok1", "conditionId": "c1", "size": size,
            "avgPrice": 0.5, "curPrice": 0.6, "cashPnl": 0.5,
            "title": "Bitcoin Up or Down - Aug 16", "outcome": "Up"}


def _make(save_log=None, **kw) -> tuple[WalletReconciler, FakeWallet, TradeState, list]:
    saved = []
    src = FakeWallet(**kw.pop("wallet_kw", {}))
    r = WalletReconciler(src, lambda: saved.append(1), **kw)
    return r, src, make_state(), saved


# ---- 余额快照 ----

def test_balance_refreshes_into_state_with_throttle():
    r, src, st, saved = _make(wallet_kw={"balance": 12.34})
    r.reconcile(1_000_000, st)
    assert st.balance == 12.34
    assert saved == [1]  # 变化 → 落盘回调
    q = src.balance_queries
    r.reconcile(1_000_005, st)  # <30s：不查
    assert src.balance_queries == q
    r.reconcile(1_000_031, st)  # ≥30s：再查
    assert src.balance_queries == q + 1


def test_balance_unchanged_does_not_save():
    r, src, st, saved = _make(wallet_kw={"balance": 12.34})
    r.reconcile(1_000_000, st)
    r.reconcile(1_000_031, st)  # 同值
    assert saved == [1]  # 第二次未落盘


def test_balance_query_failure_keeps_old_value():
    r, src, st, saved = _make(wallet_kw={"balance": RuntimeError("no creds")})
    r.reconcile(1_000_000, st)
    assert st.balance is None
    assert saved == []


def test_live_mode_captures_day_start_balance():
    r, src, st, saved = _make(dry_run=False, wallet_kw={"balance": 20.0})
    r.reconcile(1_000_000, st)
    assert st.day_start_balance == 20.0


def test_dry_run_does_not_capture_day_start_balance():
    r, src, st, saved = _make(dry_run=True, wallet_kw={"balance": 20.0})
    r.reconcile(1_000_000, st)
    assert st.day_start_balance is None  # dry-run 用交易聚合，不捕获钱包基准


def test_day_roll_resets_benchmark_then_recaptures():
    """跨天重置基准后，下一次刷新捕获新基准（回归：刷新先于跨天曾清掉基准）。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"balance": 20.0})
    r.reconcile(1_000_000, st)
    assert st.day_start_balance == 20.0
    st.roll_day("next-day")  # 引擎 tick 顺序：跨天 → reconcile
    src.balance = 25.0
    r.reconcile(1_000_000 + 24 * 3600 + 31, st)
    assert st.day_start_balance == 25.0
    assert st.balance == 25.0


# ---- 实时持仓核对（幽灵持仓） ----

def test_clears_ghost_position():
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": []})
    st.position = make_pos()
    r.reconcile(1_000_031, st)
    assert st.position is None  # 幽灵持仓清除
    assert st.live_positions == []
    assert saved  # 有落盘回调


def test_keeps_position_when_polymarket_has_it():
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": [_eth_pos()]})
    st.position = make_pos()
    r.reconcile(1_000_031, st)
    assert st.position is not None  # 保留
    assert st.live_positions == [_eth_pos()]


def test_query_failure_keeps_position():
    """查询失败（None）不核对：防误清真实持仓。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": None})
    st.position = make_pos()
    r.reconcile(1_000_031, st)
    assert st.position is not None  # 不动
    assert st.live_positions is None


def test_skipped_in_dry_run():
    r, src, st, saved = _make(dry_run=True, wallet_kw={"positions": []})
    st.position = make_pos()
    r.reconcile(1_000_031, st)
    assert st.position is not None  # 模拟持仓不核对
    assert src.position_queries == 0


def test_throttled_positions_check():
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": []})
    st.position = make_pos()
    r.reconcile(1_000_000, st)
    q = src.position_queries
    r.reconcile(1_000_005, st)  # <30s
    assert src.position_queries == q
    r.reconcile(1_000_031, st)
    assert src.position_queries == q + 1


def test_untracked_position_warns_but_keeps():
    """本地无记录但 Polymarket 有持仓：警告，不自动接管。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": [_eth_pos()]})
    r.reconcile(1_000_031, st)
    assert st.position is None  # 不接管
    assert st.live_positions == [_eth_pos()]


def test_title_alias_matching():
    """title 匹配符号或其全名（btc→bitcoin）；其他标的持仓不算本标的。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": [
        {"title": "Ethereum Up or Down - Aug 16", "outcome": "Up"},
    ]})
    st.position = make_pos()
    r.reconcile(1_000_031, st)
    assert st.position is None  # 无 BTC 持仓 → 幽灵清除


def test_reconcile_tracks_new_state_after_reset():
    """引擎 reset 重建 TradeState 后，reconcile 跟随新对象（不持有旧引用）。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"balance": 12.34})
    r.reconcile(1_000_000, st)
    st2 = TradeState(symbol="BTC", mode="dry-run")
    src.balance = 15.0
    r.reconcile(1_000_031, st2)
    assert st.balance == 12.34  # 旧对象不受影响
    assert st2.balance == 15.0
