"""统计与验证报告。

- 胜率/ROI：来自 trades.csv（每笔交易记录）
- 验证门槛：≥ min_trades 笔且跨度 ≥ min_days 天
"""

from __future__ import annotations

import math
import statistics
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
    mean_pnl: float = 0.0       # 每笔均值
    t_stat: float = 0.0         # 均值 t 统计量（H0: 期望=0；|t|≥1.96 才与噪声可区分）
    ci_low: float = 0.0         # 均值 95% 置信区间下界
    ci_high: float = 0.0        # 均值 95% 置信区间上界
    win_rate_z: float = 0.0     # 胜率 z 统计量（H0: 胜率=50%；|z|≥1.96 才显著）


def significance_stats(pnls: list[float]) -> dict:
    """盈亏序列的显著性指标（单一事实源）：每笔均值 / t / 95% CI / 胜率 z。

    依据：124 笔累计 +9.12 但 t≈1.0（95% CI 跨 0）——笔数门槛（200）之外
    还需边际显著，否则把噪声波动当优势上实盘。|t|<1.96 且 |z|<1.96 时
    报告中给出明确警示。
    """
    n = len(pnls)
    out = {"mean_pnl": 0.0, "t_stat": 0.0, "ci_low": 0.0, "ci_high": 0.0, "win_rate_z": 0.0}
    if n < 2:
        return out
    mean = statistics.fmean(pnls)
    stdev = statistics.stdev(pnls)
    se = stdev / math.sqrt(n)
    out["mean_pnl"] = mean
    if se == 0.0:
        # 常数序列（每笔盈亏相同）：样本确定性，t 趋于无穷（报告显示 inf 即完全确定）
        out["t_stat"] = float("inf") if mean > 0 else (float("-inf") if mean < 0 else 0.0)
        out["ci_low"] = out["ci_high"] = mean
    else:
        out["t_stat"] = mean / se
        out["ci_low"] = mean - 1.96 * se
        out["ci_high"] = mean + 1.96 * se
    wins = sum(1 for p in pnls if p > 0)
    p = wins / n
    out["win_rate_z"] = (p - 0.5) / math.sqrt(0.25 / n)
    return out


def aggregate(records, match=None) -> dict:
    """交易记录聚合（笔数/胜败/盈亏/最大亏损）。

    match(record) 返回 False 的行不计入（如今日过滤）；None 表示全部交易。
    记录为 TradeRecord（字段访问，坏行已在账本读面滤除）——语义曾手写在
    monitor._aggregate_trades（面板），收敛为本模块单一实现，monitor 与
    compute_stats 共用（候选 6：加一个统计维度只改一处）。
    """
    agg = {"n": 0, "wins": 0, "losses": 0, "gain": 0.0, "loss": 0.0,
           "max_loss": 0.0, "pnl": 0.0}
    for r in records:
        if match is not None and not match(r):
            continue
        p = r.pnl
        agg["n"] += 1
        agg["pnl"] += p
        if p > 0:
            agg["wins"] += 1
            agg["gain"] += p
        else:
            agg["losses"] += 1
            agg["loss"] += p
            agg["max_loss"] = min(agg["max_loss"], p)
    return agg


def compute_stats(records: list[dict]) -> Stats:
    """从交易记录计算指标。

    记录统一由 ledger.load_records 提供（实盘 API 流水配对 / 引擎业务记录），
    本函数不再自行选文件或嗅探数据源（曾按 type 列判断 api 流水——口径漂移源）。
    胜败/盈亏口径与聚合单一实现（aggregate）同源，pandas 只算跨度/窗口数。
    """
    if not records:
        return Stats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0)
    ag = aggregate(records)
    total = ag["n"]
    if total == 0:
        return Stats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0)
    wins = ag["wins"]
    total_pnl = ag["pnl"]
    total_cost = sum(r.entry_price * r.size for r in records)
    n_windows = len({r.window_start for r in records})
    sig = significance_stats([r.pnl for r in records])

    span_days = 0.0
    try:
        ts = pd.to_datetime([r.ts for r in records])
        span_days = (ts.max() - ts.min()).total_seconds() / 86400.0
    except (ValueError, TypeError, KeyError):
        pass

    return Stats(
        total_trades=total,
        wins=wins,
        losses=total - wins,  # pnl<=0（含平局）均计为负（与 aggregate.losses 同口径）
        win_rate=wins / total if total else 0.0,
        total_pnl=total_pnl,
        total_cost=total_cost,
        roi=total_pnl / total_cost if total_cost else 0.0,
        span_days=span_days,
        n_windows=n_windows,
        mean_pnl=sig["mean_pnl"],
        t_stat=sig["t_stat"],
        ci_low=sig["ci_low"],
        ci_high=sig["ci_high"],
        win_rate_z=sig["win_rate_z"],
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
    sig_text = (
        f"每笔均值 {stats.mean_pnl:+.4f}（t={stats.t_stat:.2f}，95% CI "
        f"[{stats.ci_low:+.3f}, {stats.ci_high:+.3f}]，胜率 z={stats.win_rate_z:.2f}）"
    )
    # 显著性警示：|t| / |z| 均 < 1.96 时利润与掷硬币不可区分（历史 124 笔实证）
    sig_warn = ""
    if stats.total_trades >= 30 and abs(stats.t_stat) < 1.96 and abs(stats.win_rate_z) < 1.96:
        sig_warn = "⚠️ 边际不显著：盈亏与噪声不可区分，未达上实盘的标准（|t| 与 |z| 均 < 1.96）"
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
        f"- **显著性: {sig_text}**",
        f"- {sig_warn}" if sig_warn else "",
        "",
        f"**验证状态: {'✅ 达到门槛' if done else '⏳ 未达门槛'}"
        f"（{stats.total_trades}/{min_trades} 笔，{stats.span_days:.1f}/{min_days} 天）**",
        "",
    ]
    report = "\n".join(lines)
    Path(path).write_text(report, encoding="utf-8")
    return report
