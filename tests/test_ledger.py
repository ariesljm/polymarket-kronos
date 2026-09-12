"""交易账本（ledger）测试：统一读面判据 + schema 单一事实源。

对应架构深化候选 2：monitor/stats/report 曾各自猜文件（is_file/type 列嗅探），
现在判据唯一（api_trades.csv 优先，缺回退 trades.csv）。
"""

import csv

import pytest

from pmbot.ledger import RECORD_COLUMNS, build_records, load_records


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_schema_single_source():
    """schema 单一事实源：引擎写入与流水配对共用同一列定义。"""
    from pmbot.state import TRADE_COLUMNS
    from pmbot.ledger import build_records  # noqa: F401（读面单一事实源收在账本）

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


def test_load_records_filters_by_symbol(tmp_path):
    """api 流水是全钱包的（live 每标的目录同步同一份）→ symbol 过滤只留本标的。

    不过滤曾让单标的视图串入其它标的交易、多标的聚合每笔 ×N 重复
    （实盘上线前检查发现：6 个标的目录各一份全钱包 api_trades.csv）。
    """
    api_rows = [
        {"ts": 100, "type": "trade", "side": "BUY", "size": 2, "price": 0.5,
         "usdc_size": 1.04, "condition_id": "c-eth", "title": "t",
         "slug": "eth-updown-5m-100", "outcome": "Up", "tx_hash": "b1"},
        {"ts": 200, "type": "redeem", "side": "", "size": 2, "price": "",
         "usdc_size": 2.0, "condition_id": "c-eth", "title": "t",
         "slug": "eth-updown-5m-100", "outcome": "Up", "tx_hash": "r1"},
        {"ts": 300, "type": "trade", "side": "BUY", "size": 2, "price": 0.5,
         "usdc_size": 1.04, "condition_id": "c-btc", "title": "t",
         "slug": "btc-updown-5m-300", "outcome": "Down", "tx_hash": "b2"},
        {"ts": 400, "type": "redeem", "side": "", "size": 2, "price": "",
         "usdc_size": 2.0, "condition_id": "c-btc", "title": "t",
         "slug": "btc-updown-5m-300", "outcome": "Down", "tx_hash": "r2"},
    ]
    _write_csv(tmp_path / "api_trades.csv", ["ts", "type", "side", "size", "price",
                                             "usdc_size", "condition_id", "title", "slug", "outcome", "tx_hash"], api_rows)
    assert len(load_records(tmp_path)) == 2  # 全钱包：两个标的都不过滤
    eth = load_records(tmp_path, symbol="ETH")
    assert [r.symbol for r in eth] == ["ETH"]
    assert load_records(tmp_path, symbol="DOGE") == []  # 无该标的流水


def test_load_records_source_engine_ignores_api_history(tmp_path):
    """source='engine' 强制用本 bot 引擎记录——api 流水含钱包全量历史（非本 bot 交易），
    策略统计/面板样本量必须只反映本 bot 实盘成交（实盘上线检查根因）。"""
    # api 流水有 3 条（全钱包历史），引擎记录只有 1 条
    _write_csv(tmp_path / "api_trades.csv", ["ts", "type", "side", "size", "price",
                                             "usdc_size", "condition_id", "title", "slug", "outcome", "tx_hash"], [
        {"ts": 100, "type": "trade", "side": "BUY", "size": 2, "price": 0.5,
         "usdc_size": 1.04, "condition_id": "c1", "title": "t",
         "slug": "eth-updown-5m-100", "outcome": "Up", "tx_hash": "b1"},
        {"ts": 200, "type": "redeem", "side": "", "size": 2, "price": "",
         "usdc_size": 2.0, "condition_id": "c1", "title": "t",
         "slug": "eth-updown-5m-100", "outcome": "Up", "tx_hash": "r1"},
    ])
    _write_csv(tmp_path / "trades.csv", RECORD_COLUMNS, [{
        "ts": "2026-09-12T03:42:25+00:00", "window_start": 1789184400, "symbol": "BNB",
        "direction": "down", "entry_price": "0.98", "exit_price": "0.97",
        "size": "1.02", "pnl": "-0.01", "reason": "take_profit",
    }])
    assert len(load_records(tmp_path, source="engine")) == 1  # 只读本 bot 引擎记录
    assert load_records(tmp_path, source="engine")[0].symbol == "BNB"
    assert len(load_records(tmp_path, source="auto")) == 1  # auto 仍 api 优先（对账兼容）


def test_build_records_skips_non_bot_markets(tmp_path):
    """钱包 api 流水混入非 bot 市场（ethereum-above-3000 等）→ 不构成交易记录。

    非 bot slug 首段会被 symbol_from_slug 当成标的（ETHEREUM/NEW/WILL），
    配对必须只认 bot 格式 {sym}-updown-{interval}-{epoch}。"""
    rows = [
        {"ts": 100, "type": "trade", "side": "BUY", "size": 2, "price": 0.5,
         "usdc_size": 1.04, "condition_id": "c-bot", "title": "t",
         "slug": "eth-updown-5m-100", "outcome": "Up", "tx_hash": "b1"},
        {"ts": 200, "type": "redeem", "side": "", "size": 2, "price": "",
         "usdc_size": 2.0, "condition_id": "c-bot", "title": "t",
         "slug": "eth-updown-5m-100", "outcome": "Up", "tx_hash": "r1"},
        {"ts": 300, "type": "trade", "side": "BUY", "size": 2, "price": 0.5,
         "usdc_size": 1.04, "condition_id": "c-manual", "title": "t",
         "slug": "ethereum-above-3000-on-december-1", "outcome": "Up", "tx_hash": "b2"},
        {"ts": 400, "type": "redeem", "side": "", "size": 2, "price": "",
         "usdc_size": 2.0, "condition_id": "c-manual", "title": "t",
         "slug": "ethereum-above-3000-on-december-1", "outcome": "Up", "tx_hash": "r2"},
    ]
    recs = build_records(rows)
    assert len(recs) == 1  # 只保留 bot 市场 eth-updown-5m
    assert recs[0].symbol == "ETH"


def test_load_records_empty_api_file(tmp_path):
    """api_trades.csv 只有表头（同步中断半写）→ 回退 trades.csv 的完整业务记录。

    深化（架构候选 E3）：is_file 存在性判据曾导致半写空表头静默覆盖完整
    引擎业务记录；现判据提升为「含数据行」——空表头视为未完成同步。
    """
    _write_csv(tmp_path / "api_trades.csv", ["ts", "type", "side", "size", "price",
                                             "usdc_size", "condition_id", "title", "slug", "outcome", "tx_hash"], [])
    _write_csv(tmp_path / "trades.csv", RECORD_COLUMNS, [{
        "ts": "2026-08-01T00:00:00+00:00", "window_start": 100, "symbol": "ETH",
        "direction": "up", "entry_price": "0.5", "exit_price": "0.9",
        "size": "2", "pnl": "0.8", "reason": "take_profit",
    }])
    recs = load_records(tmp_path)
    assert len(recs) == 1 and recs[0].reason == "take_profit"


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
