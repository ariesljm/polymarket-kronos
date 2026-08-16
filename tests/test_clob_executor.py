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
    assert ex.market_sell("111", 2.38) == ex.best_bid("111")  # dry-run 按 best_bid 成交


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
