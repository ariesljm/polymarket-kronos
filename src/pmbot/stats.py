"""统计与验证报告。

- 胜率/ROI：来自 trades.csv（每笔交易记录）
- 方向准确率：来自 PredictionLog（预测方向 vs 实际方向，与交易盈亏解耦）
- 验证门槛：≥ min_trades 笔且跨度 ≥ min_days 天
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

DEFAULT_MIN_TRADES = 200
DEFAULT_MIN_DAYS = 7


@dataclass
class Stats:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    total_cost: float
    roi: float
    span_days: float
    n_windows: int
    accuracy: dict


def compute_stats(trades_path: str | Path, accuracy: dict) -> Stats:
    """从 trades.csv 计算交易指标；accuracy 来自 PredictionLog。"""
    path = Path(trades_path)
    if not path.is_file():
        return Stats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, accuracy)
    df = pd.read_csv(path)
    if df.empty:
        return Stats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, accuracy)

    total = len(df)
    pnls = df["pnl"].astype(float)
    wins = int((pnls > 0).sum())
    losses = total - wins  # pnl<=0（含平局）均计为负
    total_pnl = float(pnls.sum())
    total_cost = float((df["entry_price"].astype(float) * df["size"].astype(float)).sum())
    n_windows = int(df["window_start"].nunique())

    span_days = 0.0
    try:
        ts = pd.to_datetime(df["ts"])
        span_days = (ts.max() - ts.min()).total_seconds() / 86400.0
    except (ValueError, TypeError, KeyError):
        pass

    return Stats(
        total_trades=total,
        wins=wins,
        losses=losses,
        win_rate=wins / total if total else 0.0,
        total_pnl=total_pnl,
        total_cost=total_cost,
        roi=total_pnl / total_cost if total_cost else 0.0,
        span_days=span_days,
        n_windows=n_windows,
        accuracy=accuracy,
    )


def is_validation_done(stats: Stats, min_trades: int = DEFAULT_MIN_TRADES, min_days: int = DEFAULT_MIN_DAYS) -> bool:
    """验证门槛：≥ min_trades 笔且跨度 ≥ min_days 天。"""
    return stats.total_trades >= min_trades and stats.span_days >= min_days


def write_report(
    stats: Stats,
    *,
    symbol: str,
    strategy: str,
    params: dict,
    path: str | Path,
    min_trades: int = DEFAULT_MIN_TRADES,
    min_days: int = DEFAULT_MIN_DAYS,
) -> str:
    """生成并落盘验证报告（Markdown），返回报告文本。"""
    done = is_validation_done(stats, min_trades, min_days)
    acc = stats.accuracy
    acc_text = f"{acc['accuracy']:.1%}（{acc['correct']}/{acc['total']}）" if acc["total"] else "—（暂无评估样本）"
    lines = [
        "# 验证报告",
        "",
        f"- 标的: {symbol}",
        f"- 策略: {strategy}",
        f"- 参数快照: {params}",
        f"- 交易笔数: {stats.total_trades}（胜 {stats.wins} / 负 {stats.losses}）",
        f"- 窗口数: {stats.n_windows}",
        f"- 时间跨度: {stats.span_days:.1f} 天",
        f"- **胜率: {stats.win_rate:.1%}**",
        f"- **ROI: {stats.roi:.2%}**（总盈亏 {stats.total_pnl:+.2f} / 总投入 {stats.total_cost:.2f}）",
        f"- **Kronos 方向准确率: {acc_text}**（与交易盈亏解耦）",
        "",
        f"**验证状态: {'✅ 达到门槛' if done else '⏳ 未达门槛'}"
        f"（{stats.total_trades}/{min_trades} 笔，{stats.span_days:.1f}/{min_days} 天）**",
        "",
    ]
    report = "\n".join(lines)
    Path(path).write_text(report, encoding="utf-8")
    return report
