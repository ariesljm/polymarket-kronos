"""执行器测试：SimExecutor（dry-run 模拟）与盘口解析（不碰网络/真钱）。"""

import pytest

from pmbot.book_price import best_price as _best_price
from pmbot.clob_executor import ClobExecutor, SimExecutor


def test_dry_run_place_and_sell():
    ex = SimExecutor(private_key="0x" + "0" * 64)
    oid = ex.place_limit("111", "buy", 0.45, 5.0)
    assert oid and oid.startswith("dry-run-")
    oid2 = ex.sell("111", 5.0, 0.80)
    assert oid2 and oid2.startswith("dry-run-")


def test_dry_run_market_buy_sell():
    """市价单不受 5 股/金额限制（服务端按金额换算份额，可小数）。"""
    ex = SimExecutor(private_key="0x" + "0" * 64)
    assert ex.market_buy("111", 2.38) is not None  # 小数份额
    assert ex.market_buy("111", 0.5) is not None   # 低于 5 股也放行
    r = ex.market_sell("111", 2.38)
    assert r is not None and r["price"] == ex.best_bid("111")  # dry-run 按 best_bid 成交
    assert r["order_id"] is None and ex.sell_proceeds("x", "111") is None  # 无真实订单


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
    assert ex.cancel("dry-run-abc") is True
    assert ex.cancel("anything") is True


def test_missing_key_raises_on_real_call(monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    # 屏蔽真实 .env 的自动加载，模拟无私钥环境
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    ex = ClobExecutor()
    with pytest.raises(RuntimeError, match="私钥"):
        ex._get_l1()


def test_best_price_ascending_bids():
    # bids 升序（CLOB 约定），best bid = max
    book = {"bids": [{"price": "0.30"}, {"price": "0.35"}, {"price": "0.40"}]}
    assert _best_price(book, "bids", want_max=True) == 0.40


def test_best_price_descending_asks():
    # asks 降序（CLOB 约定），best ask = min
    book = {"asks": [{"price": "0.60"}, {"price": "0.55"}, {"price": "0.50"}]}
    assert _best_price(book, "asks", want_max=False) == 0.50


def test_best_price_handles_missing_and_bad_levels():
    assert _best_price(None, "bids", want_max=True) is None
    book = {"bids": [{"price": "bad"}, {"price": "0.31"}]}
    assert _best_price(book, "bids", want_max=True) == 0.31
    assert _best_price({"bids": []}, "bids", want_max=True) is None


def test_min_shares_for_price():
    """规则组合：≥5 股且金额 ≥$1。"""
    from pmbot.clob_executor import min_shares_for_price

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
    assert fill["order_id"] == "0x953b"
    assert fill["filled_size"] == pytest.approx(1.754384)  # 实际股数（网页 1.8）
    assert fill["avg_price"] == pytest.approx(0.57)  # 实际成交价（不含费）


def test_parse_fill_taking_amount_fallback(monkeypatch):
    """响应自带 takingAmount 时直接取实际股数（无详情也准确）。"""
    ex = ClobExecutor(private_key="0x" + "0" * 64)
    monkeypatch.setattr(ex, "get_order", lambda oid: None)
    fill = ex._parse_fill({"orderID": "0x953b", "takingAmount": "1.754384",
                           "status": "matched", "success": True})
    assert fill["filled_size"] == pytest.approx(1.754384)
    assert fill["avg_price"] is None  # 无价格字段 → 调用方回退盘口价


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
    assert fill["avg_price"] == pytest.approx(0.999999 / 6.666665)  # 0.15（网页口径）
    assert fill["filled_size"] == pytest.approx(6.666665)
