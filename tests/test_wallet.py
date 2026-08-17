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
    # 默认 window_start 为很早的旧窗口（幽灵场景：窗口早已结束且超过宽限期）
    return Position(Direction.UP, 0.45, 2.0, 800, kw.pop("window_start", 995_000), **kw)


def _eth_pos(size=5.0, slug="btc-updown-15m-999900", outcome="Up", **extra):
    p = {"asset": "tok1", "conditionId": "c1", "size": size,
         "avgPrice": 0.5, "curPrice": 0.6, "cashPnl": 0.5,
         "title": "Bitcoin Up or Down - Aug 16", "outcome": outcome,
         "slug": slug}
    p.update(extra)
    return p


def _make(save_log=None, **kw) -> tuple[WalletReconciler, FakeWallet, TradeState, list]:
    saved = []
    src = FakeWallet(**kw.pop("wallet_kw", {}))
    r = WalletReconciler(src, lambda: saved.append(1), **kw)
    return r, src, make_state(), saved


# ---- 死仓（已结算归零）不保护、不接管 ----

def test_dead_position_does_not_block_ghost_clear():
    """链上只有已结算归零的死仓 → 不构成"链上有持仓"，幽灵残留照常清除。

    回归：/positions 永久保留未 redeem 的归零仓（title 匹配 ETH），本地残留
    被误判"链上有"而保护 → 占用持仓槽位，新窗口不开仓。
    """
    r, src, st, saved = _make(dry_run=False, wallet_kw={
        "positions": [_eth_pos(redeemable=True, currentValue=0.0)]})
    st.position = make_pos()  # 窗口早已结束且超宽限
    r.reconcile(1_000_500, st)
    assert st.position is None  # 幽灵清除，死仓不阻止
    assert saved


def test_dead_position_not_adopted():
    """本地无持仓 + 链上只有死仓 → 不接管（死仓占用槽位导致新窗口不开仓）。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={
        "positions": [_eth_pos(redeemable=True, currentValue=0.0)]})
    st.window_start = 999_900
    r.reconcile(1_000_500, st)
    assert st.position is None  # 不接管
    assert st.live_positions == [_eth_pos(redeemable=True, currentValue=0.0)]  # 快照仍展示


def test_settled_win_position_still_kept():
    """已结算但有残值（赢了可 redeem）→ 不算死仓，仍保护本地持仓（settle 路径记账兑付）。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={
        "positions": [_eth_pos(redeemable=True, currentValue=0.8)]})
    st.position = make_pos()
    r.reconcile(1_000_500, st)
    assert st.position is not None  # 保留


def test_unsettled_position_not_dead():
    """结果未定（redeemable=False，结算中）→ 不算死仓，正常保护/接管。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={
        "positions": [_eth_pos(redeemable=False, curPrice=0.4)]})
    st.position = make_pos()
    r.reconcile(1_000_500, st)
    assert st.position is not None  # 保留


# ---- 启动核对（startup_reconcile） ----

def test_startup_reconcile_clears_ghost_immediately():
    """启动核对：本地残留幽灵持仓（链上已无、窗口已结束）→ 当场清除，不等轮询。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": []})
    st.position = make_pos()
    r.startup_reconcile(1_000_031, st)
    assert st.position is None
    assert saved  # 有落盘回调


def test_startup_reconcile_keeps_real_position():
    """启动核对：链上仍有持仓 → 保留并继续管理（防误清真实持仓）。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": [_eth_pos()]})
    st.position = make_pos()
    r.startup_reconcile(1_000_031, st)
    assert st.position is not None
    assert st.live_positions == [_eth_pos()]


def test_startup_reconcile_skipped_in_dry_run():
    """dry-run 不启动核对（模拟持仓与真实钱包无关）。"""
    r, src, st, saved = _make(dry_run=True, wallet_kw={"positions": []})
    st.position = make_pos()
    r.startup_reconcile(1_000_031, st)
    assert st.position is not None
    assert src.position_queries == 0


def test_startup_reconcile_skipped_without_position():
    """本地无持仓：不查询（未跟踪持仓由常规 reconcile 首 tick 接管）。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": [_eth_pos()]})
    r.startup_reconcile(1_000_031, st)
    assert src.position_queries == 0


def test_startup_reconcile_bypasses_throttle():
    """启动核对绕过节流：刚 reconcile 过（节流期内）仍强制核对——启动场景必须执行。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": []})
    st.position = make_pos()
    r.reconcile(1_000_000, st)  # 已核对一次（进入节流期）
    st.position = make_pos()  # 模拟启动加载到的残留持仓
    r.startup_reconcile(1_000_005, st)
    assert src.position_queries == 2


def test_startup_reconcile_recent_position_not_cleared():
    """启动核对同样受宽限保护：窗口未结束/宽限期内不误清刚买入的持仓。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": []})
    st.position = make_pos(window_start=1_000_200)  # 窗口未结束（step 300s）
    r.startup_reconcile(1_000_250, st)
    assert st.position is not None  # 不误清
    assert st.live_positions == []


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


def test_adopts_untracked_position():
    """本地无记录但 Polymarket 有本标的持仓（成交响应丢失/误清恢复）→ 自动接管。

    接管后持仓回到 bot 管理：止损/时间止损/结算可正常触发（回归：误清后资金裸奔）。
    """
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": [_eth_pos()]})
    st.window_start = 999_900
    r.reconcile(1_000_031, st)
    assert st.position is not None
    assert st.position.direction is Direction.UP
    assert st.position.size == 5.0
    assert st.position.entry_price == 0.5
    assert st.position.window_start == 999_900  # 从 slug 解析窗口起点
    assert st.window_bet_placed is True  # 同窗口已下注，防重复买入
    assert st.live_positions == [_eth_pos()]


def test_adopt_down_position_and_old_window():
    """接管 DOWN 方向；持仓窗口早于当前窗口时不置 window_bet_placed（不误伤新窗口下注）。"""
    r, src, st, saved = _make(dry_run=False, wallet_kw={
        "positions": [_eth_pos(outcome="Down", slug="btc-updown-15m-999900")]})
    st.window_start = 1_000_800  # 当前窗口晚于持仓窗口
    r.reconcile(1_001_031, st)
    assert st.position is not None
    assert st.position.direction is Direction.DOWN
    assert st.window_bet_placed is False  # 旧窗口仓位不影响新窗口下注


def test_adopt_skips_slug_without_window():
    """slug 解析不出窗口起点（非 bot 市场格式，疑似手动仓位）→ 不接管，仅警告。"""
    pos = _eth_pos(slug="some-other-market")
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": [pos]})
    r.reconcile(1_000_031, st)
    assert st.position is None  # 不接管
    assert st.live_positions == [pos]


def test_recent_position_not_cleared_during_index_delay():
    """刚买入的持仓（窗口未结束/宽限期内）Polymarket 暂未出现 → 不清除。

    回归：买入后 /positions 索引延迟返回空，被误判幽灵清除 → 真实持仓
    失去止损/时间止损/结算管理（本地 position=null、UI 与实盘不符）。
    """
    r, src, st, saved = _make(dry_run=False, wallet_kw={"positions": []})
    # 窗口 999900（step 300s：结束 1_000_200，宽限至 1_000_380）
    st.position = make_pos(window_start=999_900)
    r.reconcile(1_000_031, st)  # 买入 31s 后核对：索引可能未同步
    assert st.position is not None  # 不误清
    assert st.live_positions == []
    # 窗口结束且超过宽限（>1_000_380）后仍无持仓 → 真幽灵，清除
    r.reconcile(1_000_500, st)
    assert st.position is None
    assert saved  # 有落盘回调


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


# ---- 候选 7：市场格式解析 + 持仓重建工厂（单一事实源） ----

def test_slug_parsers_single_source():
    from pmbot.types import direction_from_outcome, symbol_from_slug, window_start_from_slug

    assert window_start_from_slug("eth-updown-5m-1786897500") == 1786897500
    assert window_start_from_slug("eth-updown-5m-xyz") is None  # 无法解析
    assert window_start_from_slug("") is None
    assert symbol_from_slug("eth-updown-5m-1786897500") == "ETH"
    assert symbol_from_slug("") == ""
    assert direction_from_outcome("Up") is Direction.UP
    assert direction_from_outcome("down") is Direction.DOWN
    assert direction_from_outcome("?") is None


def test_rebuilt_position_factory():
    """重建工厂：entered_remaining_sec = 窗口剩余（负数截 0），三路径共用。"""
    from pmbot.types import rebuilt_position

    p = rebuilt_position(Direction.UP, 0.45, 2.0, window_start=1000, now_sec=1100, step_sec=300)
    assert p.entered_remaining_sec == 200  # 1000+300−1100
    p2 = rebuilt_position(Direction.DOWN, 0.5, 1.0, window_start=1000, now_sec=1500, step_sec=300)
    assert p2.entered_remaining_sec == 0  # 已过窗口：截 0
