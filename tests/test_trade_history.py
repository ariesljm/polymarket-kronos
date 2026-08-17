"""TradeHistorySyncer 单元测试：增量同步、去重、落盘格式（fake fetch，无网络）。"""

import csv

import pytest

from pmbot.trade_history import TradeHistorySyncer


def _mk(ts, tx, side="BUY", size=1.0, price=0.5, rtype=None, usdc=0.5):
    return {"timestamp": ts, "transactionHash": tx, "side": side, "size": size,
            "price": price, "usdcSize": usdc, "conditionId": "c1",
            "title": "Bitcoin Up or Down", "slug": "btc-updown-5m-1", "outcome": "Up",
            "type": rtype}


def _rows(path):
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _syncer(tmp_path, trades=None, redeems=None, **kw):
    pages = {}
    if trades is not None:
        pages["t"] = trades
    if redeems is not None:
        pages["r"] = redeems
    return TradeHistorySyncer(
        tmp_path / "api_trades.csv",
        lambda offset=0, limit=500: (pages.get("t") or [])[offset:offset + limit],
        lambda offset=0, limit=500: (pages.get("r") or [])[offset:offset + limit],
        **kw,
    )


def test_first_sync_pulls_all_and_writes_header(tmp_path):
    s = _syncer(tmp_path, trades=[_mk(100, "tx1"), _mk(90, "tx2")])
    assert s.sync() == 2
    rows = _rows(tmp_path / "api_trades.csv")
    assert len(rows) == 2
    assert rows[0]["tx_hash"] == "tx1"
    assert rows[0]["type"] == "trade"
    assert rows[0]["side"] == "BUY"


def test_incremental_sync_only_appends_new(tmp_path):
    s = _syncer(tmp_path, trades=[_mk(100, "tx1"), _mk(90, "tx2")])
    s.sync()
    # 模拟新流水：tx1/tx2 已同步，tx3 是新的
    s = _syncer(tmp_path, trades=[_mk(120, "tx3"), _mk(100, "tx1"), _mk(90, "tx2")])
    assert s.sync() == 1  # 只新增 tx3
    rows = _rows(tmp_path / "api_trades.csv")
    assert [r["tx_hash"] for r in rows] == ["tx1", "tx2", "tx3"]


def test_redeem_records_merged(tmp_path):
    # 真实 REDEEM 记录无 side 字段（兑付不是买卖）
    redeem = _mk(120, "tx2", rtype="REDEEM", usdc=3.0)
    del redeem["side"]
    s = _syncer(tmp_path,
                trades=[_mk(100, "tx1", side="SELL", size=2.0, price=0.9, usdc=1.8)],
                redeems=[redeem])
    s.sync()
    rows = _rows(tmp_path / "api_trades.csv")
    assert len(rows) == 2
    redeem_row = [r for r in rows if r["type"] == "redeem"][0]
    assert redeem_row["usdc_size"] == "3.0"
    assert redeem_row["side"] == ""


def test_sync_no_network_returns_zero(tmp_path):
    s = TradeHistorySyncer(tmp_path / "api_trades.csv",
                           lambda offset=0, limit=500: [],
                           lambda offset=0, limit=500: [])
    assert s.sync() == 0
    assert not (tmp_path / "api_trades.csv").exists()  # 无新增不建文件


def test_duplicate_tx_not_rewritten(tmp_path):
    """同一 transactionHash 重复出现（API 抖动）→ 不重复落盘。"""
    s = _syncer(tmp_path, trades=[_mk(100, "tx1"), _mk(100, "tx1")])
    assert s.sync() == 2  # 首拉按原样落盘（都未同步过）
    rows = _rows(tmp_path / "api_trades.csv")
    assert len(rows) == 2
    # 第二次同步：全部已知 → 0 新增
    s2 = _syncer(tmp_path, trades=[_mk(100, "tx1")])
    assert s2.sync() == 0
    assert len(_rows(tmp_path / "api_trades.csv")) == 2


def test_pagination_stops_at_known_history(tmp_path):
    """多页流水：第一页全新、第二页全是已知 → 拉到历史区即停，不无限拉取。"""
    trades = [_mk(200, "t2"), _mk(150, "t1")]  # 第一页（假想 500 上限未触发）
    s = _syncer(tmp_path, trades=trades)
    s.sync()
    # 本地已有 t1/t2；新流水 t4、t3 后接历史 t1
    s2 = _syncer(tmp_path, trades=[_mk(400, "t4"), _mk(300, "t3"), _mk(200, "t1")])
    assert s2.sync() == 2
    rows = _rows(tmp_path / "api_trades.csv")
    assert [r["tx_hash"] for r in rows] == ["t2", "t1", "t4", "t3"]


# ---- 配对聚合（build_records）：API 流水 → 交易记录 ----


def _api_buy(ts, cid, slug, outcome, size, price, usdc=None):
    return {"ts": ts, "type": "trade", "side": "BUY", "size": size, "price": price,
            "usdc_size": usdc, "condition_id": cid, "slug": slug, "outcome": outcome,
            "tx_hash": f"b-{ts}"}


def _api_sell(ts, cid, size, price, usdc=None):
    return {"ts": ts, "type": "trade", "side": "SELL", "size": size, "price": price,
            "usdc_size": usdc, "condition_id": cid, "slug": "eth-updown-5m-1786897500",
            "outcome": "Down", "tx_hash": f"s-{ts}"}


def _api_redeem(ts, cid, size, usdc):
    return {"ts": ts, "type": "redeem", "side": "", "size": size, "price": None,
            "usdc_size": usdc, "condition_id": cid, "slug": "eth-updown-5m-1786897500",
            "outcome": "Down", "tx_hash": f"r-{ts}"}


def test_build_records_sell_pair(tmp_path):
    """BUY + SELL 配对：盈亏 = 卖出收入 − 买入成本（含手续费 usdc 口径）。"""
    from pmbot.trade_history import build_records
    rows = [
        _api_buy(100, "c1", "eth-updown-5m-1786897500", "Down", 2.0, 0.5, usdc=1.04),  # 含手续费
        _api_sell(200, "c1", 2.0, 0.9, usdc=1.8),  # 卖出收入（已扣卖出手续费）
    ]
    recs = build_records(rows)
    assert len(recs) == 1
    r = recs[0]
    assert r.direction == "down"
    assert r.symbol == "ETH"
    assert r.window_start == 1786897500
    assert r.size == pytest.approx(2.0)
    assert r.pnl == pytest.approx(1.8 - 1.04)  # 收入 − 成本（真实口径）
    assert r.reason == "sell"
    assert r.entry_price == pytest.approx(1.04 / 2.0)
    assert r.exit_price == pytest.approx(1.8 / 2.0)


def test_build_records_settle_pair(tmp_path):
    """BUY + REDEEM 配对：结算兑付（usdc_size = 实际到账）→ reason=settle。"""
    from pmbot.trade_history import build_records
    rows = [
        _api_buy(100, "c1", "eth-updown-5m-1786897500", "Up", 2.0, 0.45, usdc=0.94),
        _api_redeem(300, "c1", 2.0, 2.0),  # 赢：每份兑 1 USDC
    ]
    recs = build_records(rows)
    assert len(recs) == 1
    assert recs[0].reason == "settle"
    assert recs[0].pnl == pytest.approx(2.0 - 0.94)


def test_build_records_open_window_skipped(tmp_path):
    """进行中窗口（有 BUY 无出场）→ 不构成交易记录。"""
    from pmbot.trade_history import build_records
    rows = [_api_buy(100, "c1", "eth-updown-5m-1786897500", "Down", 2.0, 0.5, usdc=1.04)]
    assert build_records(rows) == []


def test_build_records_usdc_fallback(tmp_path):
    """usdc_size 缺失（旧数据/无字段）→ 回退 size×price。"""
    from pmbot.trade_history import build_records
    rows = [
        _api_buy(100, "c1", "eth-updown-5m-1786897500", "Down", 2.0, 0.5),  # 无 usdc
        _api_sell(200, "c1", 2.0, 0.9),  # 无 usdc
    ]
    recs = build_records(rows)
    assert recs[0].pnl == pytest.approx(2.0 * 0.9 - 2.0 * 0.5)


def test_build_records_partial_sell_plus_redeem(tmp_path):
    """部分卖出 + 剩余结算兑付 → 合并为一笔（收入 = 卖出 + 兑付）。"""
    from pmbot.trade_history import build_records
    rows = [
        _api_buy(100, "c1", "eth-updown-5m-1786897500", "Down", 2.0, 0.5, usdc=1.04),
        _api_sell(200, "c1", 1.0, 0.9, usdc=0.9),
        _api_redeem(300, "c1", 1.0, 1.0),
    ]
    recs = build_records(rows)
    assert len(recs) == 1
    assert recs[0].reason == "sell"  # 组内有 SELL
    assert recs[0].pnl == pytest.approx(0.9 + 1.0 - 1.04)


def test_build_records_multiple_windows(tmp_path):
    """多窗口多笔 → 按 ts 升序返回多条记录。"""
    from pmbot.trade_history import build_records
    rows = [
        _api_buy(100, "c2", "eth-updown-5m-100", "Up", 1.0, 0.5, usdc=0.52),
        _api_redeem(150, "c2", 1.0, 1.0),
        _api_buy(200, "c1", "eth-updown-5m-200", "Down", 2.0, 0.4, usdc=0.84),
        _api_sell(250, "c1", 2.0, 0.7, usdc=1.4),
    ]
    recs = build_records(rows)
    assert len(recs) == 2
    assert [r.ts for r in recs] == sorted(r.ts for r in recs)
