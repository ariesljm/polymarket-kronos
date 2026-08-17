"""交易账本（ledger）测试：统一读面判据 + schema 单一事实源。

对应架构深化候选 2：monitor/stats/report 曾各自猜文件（is_file/type 列嗅探），
现在判据唯一（api_trades.csv 优先，缺回退 trades.csv）。
"""

import csv

import pytest

from pmbot.ledger import RECORD_COLUMNS, load_records


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_schema_single_source():
    """schema 单一事实源：引擎写入与流水配对共用同一列定义。"""
    from pmbot.state import TRADE_COLUMNS
    from pmbot.trade_history import build_records  # noqa: F401（模块可导入即引用成立）

    assert TRADE_COLUMNS == RECORD_COLUMNS
    assert RECORD_COLUMNS == [
        "ts", "window_start", "symbol", "direction",
        "entry_price", "exit_price", "size", "pnl", "reason",
    ]


def test_load_records_uses_api_trades_first(tmp_path):
    """api_trades.csv 存在 → 配对成交易记录（含手续费口径），不读 trades.csv。"""
    _write_csv(tmp_path / "trades.csv", RECORD_COLUMNS, [{
        "ts": "2026-08-01T00:00:00+00:00", "window_start": 100, "symbol": "ETH",
        "direction": "up", "entry_price": "0.5", "exit_price": "1.0",
        "size": "2", "pnl": "1.0", "reason": "take_profit",
    }])
    _write_csv(tmp_path / "api_trades.csv", ["ts", "type", "side", "size", "price",
                                             "usdc_size", "condition_id", "title", "slug", "outcome", "tx_hash"], [
        {"ts": 100, "type": "trade", "side": "BUY", "size": 2, "price": 0.5,
         "usdc_size": 1.04, "condition_id": "c1", "title": "t", "slug": "eth-updown-5m-100", "outcome": "Up", "tx_hash": "b1"},
        {"ts": 200, "type": "redeem", "side": "", "size": 2, "price": "",
         "usdc_size": 2.0, "condition_id": "c1", "title": "t", "slug": "eth-updown-5m-100", "outcome": "Up", "tx_hash": "r1"},
    ])
    recs = load_records(tmp_path)
    assert len(recs) == 1
    assert recs[0].reason == "settle"
    assert recs[0].pnl == pytest.approx(2.0 - 1.04)  # API 口径（含手续费）


def test_load_records_falls_back_to_trades_csv(tmp_path):
    """无 api_trades.csv → 回退 trades.csv（引擎业务记录原样读）。"""
    _write_csv(tmp_path / "trades.csv", RECORD_COLUMNS, [{
        "ts": "2026-08-01T00:00:00+00:00", "window_start": 100, "symbol": "ETH",
        "direction": "up", "entry_price": "0.5", "exit_price": "0.9",
        "size": "2", "pnl": "0.8", "reason": "take_profit",
    }])
    recs = load_records(tmp_path)
    assert len(recs) == 1
    assert recs[0].reason == "take_profit"


def test_load_records_empty_dir(tmp_path):
    """两个文件都不存在 → 空列表（消费方无文件也可安全调用）。"""
    assert load_records(tmp_path) == []


def test_load_records_empty_api_file(tmp_path):
    """api_trades.csv 只有表头 → 空列表（不误读 trades.csv）。"""
    _write_csv(tmp_path / "api_trades.csv", ["ts", "type", "side", "size", "price",
                                             "usdc_size", "condition_id", "title", "slug", "outcome", "tx_hash"], [])
    _write_csv(tmp_path / "trades.csv", RECORD_COLUMNS, [{
        "ts": "2026-08-01T00:00:00+00:00", "window_start": 100, "symbol": "ETH",
        "direction": "up", "entry_price": "0.5", "exit_price": "0.9",
        "size": "2", "pnl": "0.8", "reason": "take_profit",
    }])
    assert load_records(tmp_path) == []


def test_records_from_csv_skips_bad_rows(tmp_path):
    """坏行（缺列/坏数值）在账本读面过滤，不进消费方（原面板坏行跳过语义迁移）。"""
    rows = [
        {"ts": "2026-08-01T00:00:00+00:00", "window_start": "100", "symbol": "ETH",
         "direction": "up", "entry_price": "0.5", "exit_price": "0.9",
         "size": "2", "pnl": "0.8", "reason": "take_profit"},
        {"ts": "bad-ts", "window_start": "100", "symbol": "ETH",  # 半写行
         "direction": "up"},
        {"pnl": "x"},  # 全缺
        {"ts": "2026-08-01T00:15:00+00:00", "window_start": "100", "symbol": "ETH",
         "direction": "down", "entry_price": "0.4", "exit_price": "0.0",
         "size": "1", "pnl": "-0.4", "reason": "settle"},
    ]
    _write_csv(tmp_path / "trades.csv", RECORD_COLUMNS, rows)
    recs = load_records(tmp_path)
    assert len(recs) == 2  # 只保留好行
    assert recs[1].direction == "down"
    assert recs[1].pnl == pytest.approx(-0.4)
