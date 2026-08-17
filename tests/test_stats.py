"""统计模块测试：胜率/ROI/方向准确率 + 验证门槛 + 报告生成。"""

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pmbot.stats import compute_stats, is_validation_done, write_report


def make_trades(path: Path, rows: list[dict]):
    fields = ["ts", "window_start", "symbol", "direction", "entry_price", "exit_price", "size", "pnl", "reason"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def trade_row(ts, pnl, entry=0.45, size=5.0, direction="up"):
    return {
        "ts": ts,
        "window_start": "1000000",
        "symbol": "BTC",
        "direction": direction,
        "entry_price": entry,
        "exit_price": round(entry + pnl / size, 4),
        "size": size,
        "pnl": pnl,
        "reason": "settle",
    }


def read_records(path):
    """CSV → TradeRecord 列表（复用账本读面转换，坏行跳过）。"""
    from pmbot.ledger import records_from_csv

    return records_from_csv(path)


def test_empty_trades_gives_zero_stats(tmp_path):
    stats = compute_stats([], accuracy={"correct": 0, "total": 0, "accuracy": 0.0})
    assert stats.total_trades == 0
    assert stats.win_rate == 0.0
    assert stats.roi == 0.0


def test_win_rate_and_roi(tmp_path):
    p = Path(tmp_path) / "trades.csv"
    rows = [
        trade_row("2026-08-01T00:00:00+00:00", +0.55),  # 赢：5×0.11=0.55
        trade_row("2026-08-01T00:15:00+00:00", +0.55),
        trade_row("2026-08-01T00:30:00+00:00", +0.55),
        trade_row("2026-08-01T00:45:00+00:00", -2.25),  # 输：全损 5×0.45
        trade_row("2026-08-01T01:00:00+00:00", -2.25),
    ]
    make_trades(p, rows)
    stats = compute_stats(read_records(p), accuracy={"correct": 3, "total": 5, "accuracy": 0.6})
    assert stats.total_trades == 5
    assert stats.wins == 3
    assert stats.losses == 2
    assert stats.n_windows == 1  # 同一窗口 5 笔
    assert stats.win_rate == pytest.approx(0.6)
    # total_cost = 5 × 0.45 × 5 = 11.25；total_pnl = 3×0.55 - 2×2.25 = -2.85
    assert stats.total_cost == pytest.approx(11.25)
    assert stats.total_pnl == pytest.approx(-2.85)
    assert stats.roi == pytest.approx(-2.85 / 11.25)
    assert stats.accuracy["correct"] == 3


def test_accuracy_come_from_prediction_log(tmp_path):
    p = Path(tmp_path) / "trades.csv"
    make_trades(p, [trade_row("2026-08-01T00:00:00+00:00", +0.55)])
    stats = compute_stats(read_records(p), accuracy={"correct": 10, "total": 20, "accuracy": 0.5})
    assert stats.accuracy == {"correct": 10, "total": 20, "accuracy": 0.5}


def test_validation_threshold_trades(tmp_path):
    p = Path(tmp_path) / "trades.csv"
    # 200 笔分布到 8 天（≥7 天）
    rows = [
        trade_row(f"2026-08-{d + 1:02d}T00:{(i % 60):02d}:00+00:00", 0.1)
        for d in range(8) for i in range(25)
    ]
    make_trades(p, rows)
    stats = compute_stats(read_records(p), accuracy={"correct": 0, "total": 0, "accuracy": 0.0})
    assert is_validation_done(stats, min_trades=200, min_days=7) is True
    # 199 笔
    make_trades(p, rows[:199])
    stats = compute_stats(read_records(p), accuracy={"correct": 0, "total": 0, "accuracy": 0.0})
    assert is_validation_done(stats, min_trades=200, min_days=7) is False


def test_validation_threshold_days(tmp_path):
    p = Path(tmp_path) / "trades.csv"
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    # 只有 5 天跨度（每天 40 笔 = 200 笔）
    rows = [
        trade_row((base + timedelta(days=d, hours=h)).isoformat(), 0.1)
        for d in range(5) for h in range(40)
    ]
    make_trades(p, rows)
    stats = compute_stats(read_records(p), accuracy={"correct": 0, "total": 0, "accuracy": 0.0})
    assert stats.span_days < 7.0  # 跨度不足 7 天
    assert is_validation_done(stats, min_trades=200, min_days=7) is False  # 笔数够但天数不够


def test_report_contains_metrics_and_config(tmp_path):
    p = Path(tmp_path) / "trades.csv"
    make_trades(p, [trade_row("2026-08-01T00:00:00+00:00", +0.55)])
    stats = compute_stats(read_records(p), accuracy={"correct": 1, "total": 2, "accuracy": 0.5})
    report = write_report(stats, symbol="BTC", strategy="kronos",
                          params={"p_up_buy": 0.60, "limit_price": 0.45}, path=Path(tmp_path) / "report.md")
    assert "BTC" in report
    assert "胜率" in report
    assert "ROI" in report
    assert "方向准确率" in report
    assert "kronos" in report
    assert "p_up_buy" in report
    assert Path(tmp_path, "report.md").is_file()


def test_aggregate_skips_bad_rows():
    """聚合单实现：坏行跳过、今日 match 过滤（monitor 面板与验证报告同口径）。"""
    from pmbot.stats import aggregate

    from pmbot.ledger import TradeRecord

    records = [
        TradeRecord(ts="2026-08-14T00:00:00+00:00", window_start=1, symbol="BC", direction="up",
                    entry_price=0.5, exit_price=0.6, size=5.0, pnl=0.55, reason="settle"),
        TradeRecord(ts="2026-08-14T00:15:00+00:00", window_start=1, symbol="BC", direction="up",
                    entry_price=0.5, exit_price=0.2, size=5.0, pnl=-2.25, reason="settle"),
        TradeRecord(ts="2026-08-14T00:30:00+00:00", window_start=1, symbol="BC", direction="up",
                    entry_price=0.5, exit_price=0.6, size=5.0, pnl=0.10, reason="settle"),
    ]
    agg = aggregate(records)
    assert agg["n"] == 3
    assert agg["wins"] == 2
    assert agg["losses"] == 1
    assert agg["gain"] == pytest.approx(0.65)
    assert agg["loss"] == pytest.approx(-2.25)
    assert agg["max_loss"] == pytest.approx(-2.25)
    assert agg["pnl"] == pytest.approx(-1.60)

    def today_only(r):
        return r.ts.startswith("2026-08-14")

    rows = [
        TradeRecord(ts="2026-08-14T00:00:00+00:00", window_start=1, symbol="BC", direction="up",
                    entry_price=0.5, exit_price=0.6, size=5.0, pnl=1.0, reason="settle"),
        TradeRecord(ts="2026-08-15T00:00:00+00:00", window_start=1, symbol="BC", direction="up",
                    entry_price=0.5, exit_price=0.6, size=5.0, pnl=0.5, reason="settle"),
    ]
    assert aggregate(rows, match=today_only)["n"] == 1
