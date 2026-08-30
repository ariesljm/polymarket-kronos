"""执行器测试：SimExecutor（dry-run 模拟）与盘口解析（不碰网络/真钱）。"""

import pytest

from pmbot.clob_executor import ClobExecutor, SimExecutor
from pmbot.executor_protocols import min_shares_for_price


def test_dry_run_place_and_sell():
    ex = SimExecutor(private_key="0x" + "0" * 64)
    oid = ex.place_limit("111", "buy", 0.45, 5.0)
    assert oid and oid.startswith("sim-")
    oid2 = ex.sell("111", 5.0, 0.80)
    assert oid2 and oid2.startswith("sim-")


def test_dry_run_market_buy_sell(monkeypatch):
    """市价单不受 5 股/金额限制（服务端按金额换算份额，可小数）。"""
    ex = SimExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "best_ask", lambda t, size=5.0: 0.50)  # 无盘口时注入报价
    monkeypatch.setattr(ex, "best_bid", lambda t, size=5.0: 0.30)
    assert ex.market_buy("111", 2.38) is not None  # 小数份额
    assert ex.market_buy("111", 0.5) is not None   # 低于 5 股也放行
    r = ex.market_sell("111", 2.38)
    assert r is not None and r.avg_price == 0.30  # dry-run 按 best_bid 成交
    assert r.order_id is None and ex.sell_proceeds("x", "111") is None  # 无真实订单


def test_dry_run_market_buy_without_book_returns_none(monkeypatch):
    """dry-run 无盘口报价 → 不建仓（与实盘“缺成交数据放弃建仓”同语义）。"""
    ex = SimExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "best_ask", lambda t, size=5.0: None)
    assert ex.market_buy("111", 2.38) is None


def test_order_rules_enforced():
    """Polymarket 规则：最少 5 股、最低 $1（实测验证）。"""
    ex = SimExecutor(private_key="0x" + "0" * 64)
    # 少于 5 股 → 拒绝
    with pytest.raises(ValueError, match="5 股"):
        ex.place_limit("111", "buy", 0.45, 2.0)
    # 金额 < $1 → 拒绝
    with pytest.raises(ValueError, match="\$1"):
        ex.place_limit("111", "buy", 0.10, 5.0)  # 5×0.10=$0.5
    # 合法单通过
    assert ex.place_limit("111", "buy", 0.45, 5.0) is not None  # 5×0.45=$2.25
    assert ex.place_limit("111", "buy", 0.10, 10.0) is not None  # 10×0.10=$1.0


def test_dry_run_cancel():
    ex = SimExecutor(private_key="0x" + "0" * 64)
    assert ex.cancel("sim-abc") is True
    assert ex.cancel("anything") is True


def test_missing_key_raises_on_real_call(monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    # 屏蔽真实 .env 的自动加载，模拟无私钥环境
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    ex = ClobExecutor()
    with pytest.raises(RuntimeError, match="私钥"):
        ex._get_l1()


def test_min_shares_for_price():
    """规则组合：≥5 股且金额 ≥$1。"""
    assert min_shares_for_price(0.45) == 5    # 5×0.45=2.25 ≥ 1
    assert min_shares_for_price(0.20) == 5    # 5×0.20=1.0 恰好
    assert min_shares_for_price(0.10) == 10   # 5×0.10=0.5 < 1 → 10 股
    assert min_shares_for_price(0.30) == 5
    assert min_shares_for_price(0.05) == 20   # 20×0.05=1.0


def test_weighted_price_filters_dust_orders():
    """微小量挂单不污染：按可成交量（默认 5 股）累计加权均价。

    场景：asks 最优档 0.009 只有 1 股，买 5 股实际成本是前几档的加权均价。
    """
    from pmbot.book_price import weighted_price as _weighted_price

    book = {"asks": [
        {"price": "0.999", "size": "15"},
        {"price": "0.19", "size": "5"},
        {"price": "0.009", "size": "1"},  # 垃圾小单
    ]}
    # 从最便宜（0.009×1 股）累计：0.009×1 + 0.19×4 = 0.769 → /5 = 0.1538
    assert _weighted_price(book, "asks", size=5) == 0.1538
    # bids 从最高价累计
    book2 = {"bids": [
        {"price": "0.001", "size": "1"},   # 垃圾小单
        {"price": "0.15", "size": "5"},
        {"price": "0.18", "size": "10"},
    ]}
    # 0.18×5 = 0.9 → /5 = 0.18
    assert _weighted_price(book2, "bids", size=5) == 0.18


def test_weighted_price_insufficient_liquidity():
    from pmbot.book_price import weighted_price as _weighted_price

    book = {"asks": [{"price": "0.10", "size": "2"}]}  # 总量 2 < 5
    assert _weighted_price(book, "asks", size=5) is None
    assert _weighted_price(None, "asks", size=5) is None


def test_parse_fill_uses_order_detail(monkeypatch):
    """实际成交数据：get_order 详情 price/size_matched 优先（服务端权威）。

    回归：16:53 实盘买入后详情返回 price=0.57/size_matched=1.754384，但旧解析
    只认 averagePrice/matchedAmount 键 → 回退盘口估算（1.9231 股 @ 0.52），
    结算兑付 1.75 与持仓记录对不上（网页实际：1.8 份 @ 0.57）。
    """
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "get_order", lambda oid: {
        "id": "0x953b", "price": "0.57", "size_matched": "1.754384",
        "status": "MATCHED", "side": "BUY",
    })
    fill = ex._parse_fill({"orderID": "0x953b", "status": "matched"})
    assert fill.order_id == "0x953b"
    assert fill.filled_size == pytest.approx(1.754384)  # 实际股数（网页 1.8）
    assert fill.avg_price == pytest.approx(0.57)  # 实际成交价（不含费）


def test_parse_fill_taking_amount_fallback(monkeypatch):
    """响应自带 takingAmount 时直接取实际股数（无详情也准确）。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "get_order", lambda oid: None)
    fill = ex._parse_fill({"orderID": "0x953b", "takingAmount": "1.754384",
                           "status": "matched", "success": True})
    assert fill.filled_size == pytest.approx(1.754384)
    assert fill.avg_price is None  # 无价格字段 → 调用方回退盘口价


def test_parse_fill_making_taking_actual_price(monkeypatch):
    """市价单响应 making/taking 金额 → 实际成交价（付/得），非订单保护价。

    回归：17:46 实盘买入 making=0.999999 taking=6.666665 → 实际 0.15，
    旧逻辑取不到 avg_price 字段回退订单详情 price（0.16 保护价），
    与 Polymarket 网页成交价（0.15）不对齐。
    """
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "get_order", lambda oid: {"price": "0.16", "size_matched": "6.666665"})
    fill = ex._parse_fill({"orderID": "0x953b", "makingAmount": "0.999999",
                           "takingAmount": "6.666665", "status": "matched"})
    assert fill.avg_price == pytest.approx(0.999999 / 6.666665)  # 0.15（网页口径）
    assert fill.filled_size == pytest.approx(6.666665)


def test_parse_fill_sell_making_taking_direction(monkeypatch):
    """卖单响应 making/taking 方向与买单相反：价 = taking/making（收到的 USDC/卖出的 token）。

    回归：20:39 time_stop 平仓卖出 1.9608 股收到 0.549 USDC（真实价 0.28），
    旧解析 making/taking = 3.571（=1/0.28）→ exit_price 记错，UI 显示 357 美分。
    """
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "get_order", lambda oid: None)
    fill = ex._parse_fill({"orderID": "0xabc", "makingAmount": "1.9608",
                           "takingAmount": "0.549", "status": "matched"}, side="sell")
    assert fill.avg_price == pytest.approx(0.549 / 1.9608)  # 0.28（网页口径）
    assert fill.filled_size == pytest.approx(1.9608)  # 卖单股数 = makingAmount


class _FakeClient:
    """最小 clob 客户端替身：可配置市价单响应。"""

    def __init__(self, resp):
        self.resp = resp

    def create_and_post_market_order(self, *a, **k):
        return self.resp


def test_market_buy_missing_data_returns_none(monkeypatch):
    """实盘市价买入成交但 API 缺实际成交数据（avg/size 缺）→ 放弃建仓（None）。

    对应架构深化候选 3：该分支曾藏在 main_loop._exec_place_market 且经现有
    interface 不可测（FakeExecutor 永远返回完整 dict）；已收进执行器成为
    成交语义的一部分，可注入缺字段响应直测。
    """
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "_get_client",
                        lambda: _FakeClient({"orderID": "oid-1", "status": "matched"}))
    assert ex.market_buy("111", 1.0) is None


def test_market_sell_falls_back_to_best_bid(monkeypatch):
    """卖单价取不到 → 执行器回退 best_bid（不再由调用方各自回退）。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "_get_client",
                        lambda: _FakeClient({"orderID": "oid-2", "status": "matched"}))
    monkeypatch.setattr(ex, "best_bid", lambda t, size=5.0: 0.40)
    fill = ex.market_sell("111", 2.0)
    assert fill is not None
    assert fill.order_id == "oid-2"
    assert fill.avg_price == pytest.approx(0.40)


# ---- 盘口新鲜度（A）+ 定价量级（C） ----


class FakeSampler:
    """采样器替身：可注入快照与年龄（snapshot_age 直接返回预设值）。"""

    def __init__(self):
        self._snaps = {}
        self._ages = {}

    def attach(self, token, book, age):
        self._snaps[token] = book
        self._ages[token] = age

    def snapshot(self, token):
        return dict(self._snaps[token]) if token in self._snaps else None

    def is_fresh(self, token):
        """陈旧判定单一事实源（与 BookSampler.is_fresh 同语义）。"""
        from pmbot.book_sampler import STALE_AGE_SEC

        age = self._ages.get(token)
        return age is not None and age <= STALE_AGE_SEC

    def update_snapshot(self, token, book):
        self._snaps[token] = book
        self._ages[token] = 0.0  # 回填后视为新鲜


def _book(ask=0.50, bid=0.48):
    return {"bids": [{"price": f"{bid:.2f}", "size": "10"}],
            "asks": [{"price": f"{ask:.2f}", "size": "10"}]}


def test_best_ask_fresh_snapshot_no_rest(monkeypatch):
    """快照新鲜（age ≤ STALE_AGE_SEC）→ 直接用快照，不触发 REST。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    s = FakeSampler()
    s.attach("tok-a", _book(ask=0.50), 0.1)
    ex.attach_sampler(s)
    rest_calls = {"n": 0}
    monkeypatch.setattr(ex, "fetch_book",
                        lambda t: rest_calls.__setitem__("n", rest_calls["n"] + 1) or _book(ask=0.90))
    assert ex.best_ask("tok-a") == 0.50
    assert ex.best_ask("tok-a", size=1.0) == 0.50
    assert rest_calls["n"] == 0


def test_best_ask_stale_snapshot_refreshes_rest(monkeypatch):
    """快照陈旧（age > STALE_AGE_SEC）→ REST 现拉新价并回填采样器（防下 tick 重复查询）。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    s = FakeSampler()
    s.attach("tok-a", _book(ask=0.10), 30.0)  # 陈旧
    ex.attach_sampler(s)
    monkeypatch.setattr(ex, "fetch_book", lambda t: _book(ask=0.60))
    assert ex.best_ask("tok-a") == 0.60  # 新价（不再用 0.10 旧快照）
    assert s._snaps["tok-a"]["asks"][0]["price"] == "0.60"  # 已回填
    assert s._ages["tok-a"] == 0.0  # 回填后新鲜


def test_best_ask_stale_rest_failure_returns_none(monkeypatch):
    """快照陈旧且 REST 失败 → 返回 None（宁缺毋滥，不报误导价，下 tick 重试）。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    s = FakeSampler()
    s.attach("tok-a", _book(ask=0.10), 30.0)  # 陈旧
    ex.attach_sampler(s)

    def boom(t):
        raise RuntimeError("network down")

    monkeypatch.setattr(ex, "fetch_book", boom)
    assert ex.best_ask("tok-a") is None


def test_best_ask_missing_snapshot_uses_rest(monkeypatch):
    """无快照（窗口切换/订阅失败）→ REST 查询返回新价。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    s = FakeSampler()  # 无任何快照
    ex.attach_sampler(s)
    monkeypatch.setattr(ex, "fetch_book", lambda t: _book(ask=0.60))
    assert ex.best_ask("tok-a") == 0.60
    assert ex.best_ask("no-sampler-token") == 0.60  # 无采样器也走 REST


def test_best_ask_size_param():
    """size 参数：定价基准按可成交量加权（C 项：小单/持仓量级决定吃到哪一档）。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    s = FakeSampler()
    # asks: 0.50×8 股 + 0.60×2 股
    s.attach("tok-a", {"bids": [], "asks": [
        {"price": "0.50", "size": "8"}, {"price": "0.60", "size": "2"}]}, 0.1)
    ex.attach_sampler(s)
    assert ex.best_ask("tok-a", size=5.0) == 0.50   # 5 股只吃第一档
    assert ex.best_ask("tok-a", size=10.0) == 0.52  # 10 股吃穿两档: (0.5×8+0.6×2)/10
    # bids 同理
    s.attach("tok-b", {"bids": [
        {"price": "0.90", "size": "4"}, {"price": "0.80", "size": "6"}], "asks": []}, 0.1)
    assert ex.best_bid("tok-b", size=4.0) == 0.90
    assert ex.best_bid("tok-b", size=10.0) == 0.84  # (0.9×4+0.8×6)/10


def test_best_bid_stale_refreshes_rest(monkeypatch):
    """best_bid 同样做新鲜度检查：陈旧 → REST 刷新（平仓决策价时效保证）。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    s = FakeSampler()
    s.attach("tok-a", _book(bid=0.20), 30.0)  # 陈旧
    ex.attach_sampler(s)
    monkeypatch.setattr(ex, "fetch_book", lambda t: _book(bid=0.70))
    assert ex.best_bid("tok-a") == 0.70
    assert s._snaps["tok-a"]["bids"][0]["price"] == "0.70"


def test_market_sell_fallback_uses_position_size(monkeypatch):
    """市场卖价取不到 → 回退 best_bid 按持仓股数量级定价（size 透传）。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    seen = {}
    monkeypatch.setattr(ex, "_get_client",
                        lambda: _FakeClient({"orderID": "oid-3", "status": "matched"}))
    monkeypatch.setattr(ex, "best_bid", lambda t, size=5.0: seen.__setitem__("size", size) or 0.40)
    fill = ex.market_sell("111", 2.0)
    assert fill is not None and fill.avg_price == pytest.approx(0.40)
    assert seen["size"] == 2.0  # 卖单股数透传给 best_bid


def test_shares_for_amount_shared_formula():
    """份额换算单一公式（目标份额与模拟成交 filled_size 共用）。"""
    from pmbot.executor_protocols import shares_for_amount

    assert shares_for_amount(1.0, 0.5) == 2.0
    assert shares_for_amount(1.0, 0.2) == 5.0
    assert shares_for_amount(0.5, 0.4) == 1.25


def test_sim_sell_price_fallback_matches_live():
    """模拟卖出价回退 0.0（与实盘 market_sell 的 or 0.0 兜底一致，防成交语义分歧）。

    回归：模拟侧曾返回 avg_price=None 的 Fill，与实盘“取不到价回退 0.0”不一致。"""
    from pmbot.clob_executor import SimExecutor

    ex = SimExecutor()
    ex._live = type("L", (), {"best_bid": lambda self, t, size=5.0: None})()
    fill = ex.market_sell("tok", 2.0)
    assert fill.avg_price == 0.0
