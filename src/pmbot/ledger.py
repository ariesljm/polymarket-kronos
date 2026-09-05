"""交易账本：交易记录的类型化载体、统一读面与 schema 单一事实源。

背景（架构深化候选 2）："一笔交易的盈亏"曾有两套并行语义——引擎实时记账
（trades.csv，含 take_profit/stop_loss 等离场原因）与 API 真实流水配对
（api_trades.csv → build_records，含手续费 usdc_size 口径）。monitor / stats /
report 三个消费方各自决定读哪个文件（is_file 存在性 / type 列嗅探 / 存在性
回退），口径静默漂移；9 列 schema 在 state.TRADE_COLUMNS（写）与
trade_history.RECORD_COLUMNS（手抄）两处维护；消费方各自裸 dict 键访问 +
本地 float() 防御转换。

本模块（候选 2 + 候选 6 收敛）：
- RECORD_COLUMNS：交易记录 schema 唯一出处（引擎写入与流水配对共用）；
- TradeRecord：类型化记录载体（消费方字段访问，键漂移静态检查即爆）；
- load_records：统一读面——api_trades.csv（真实流水配对，含手续费）优先，
  缺回退 trades.csv（引擎业务记录）；坏行（缺列/坏数值）在读到边界跳过，
  消费方不再各自防御。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pmbot.types import symbol_from_slug, window_start_from_slug

# 交易记录 schema（trades.csv 写入 / api 流水配对 / 展示统计共用，单一事实源）
RECORD_COLUMNS = [
    "ts",
    "window_start",
    "symbol",
    "direction",
    "entry_price",
    "exit_price",
    "size",
    "pnl",
    "reason",
]


@dataclass(frozen=True)
class TradeRecord:
    """一笔已平仓交易（类型化载体：消费方访问字段而非魔法键）。

    ts: ISO 时间戳（UTC）；window_start: 所属窗口起点秒；
    direction: up/down；reason: 离场原因（take_profit/stop_loss/settle/...）。
    """

    ts: str
    window_start: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    reason: str


def records_from_csv(path: str | Path) -> list[TradeRecord]:
    """trades.csv 行 → TradeRecord；坏行（缺列/坏数值）跳过（转换在读到边界）。"""
    records: list[TradeRecord] = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                records.append(TradeRecord(
                    ts=row["ts"],
                    window_start=int(float(row["window_start"])),
                    symbol=row["symbol"],
                    direction=row["direction"],
                    entry_price=float(row["entry_price"]),
                    exit_price=float(row["exit_price"]),
                    size=float(row["size"]),
                    pnl=float(row["pnl"]),
                    reason=row["reason"],
                ))
            except (KeyError, ValueError, TypeError):
                continue  # 坏行（半写/空行/缺字段）不在消费方重复防御
    return records


def load_records(data_dir: str | Path) -> list[TradeRecord]:
    """统一读面：返回 TradeRecord 列表（api 流水配对优先，引擎记录回退）。

    判据唯一：api_trades.csv 存在且含数据行（同步中断/半写会留下空表头，
    此时回退 trades.csv 的完整引擎业务记录）才优先；否则回退 trades.csv；
    两者都不存在返回 []。
    """
    data_dir = Path(data_dir)
    api = data_dir / "api_trades.csv"
    if api.is_file():
        with open(api, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows:
            return build_records(rows)
    trades = data_dir / "trades.csv"
    if trades.is_file():
        return records_from_csv(trades)
    return []


def build_records(rows: list[dict]) -> list["TradeRecord"]:
    """API 流水 → 交易记录（配对聚合，TradeRecord 类型化载体）。

    配对键 = conditionId（同一市场的买入/卖出/兑付归为一笔）：
    - 成本 = Σ BUY 金额（usdc_size，缺回退 size×price）
    - 收入 = Σ SELL 金额 + Σ REDEEM 到账（usdc_size，缺回退 size×price）
    - 盈亏 = 收入 − 成本（**含手续费的真实口径**）；进行中窗口（有 BUY 无出场）跳过
    - ts = 组内最后流水时间（ISO）；direction = 买入 outcome；
      reason：组内有 SELL → sell，仅 REDEEM → settle（API 无法区分止盈/止损）

    返回按 ts 升序的记录；坏行（无 conditionId/窗口解析失败）跳过。
    纯读侧关注点收在账本（写模块 trade_history 只做增量落盘，不再反向
    import 本模块——曾 ledger ↔ trade_history 循环依赖）。
    """
    groups: dict[str, dict] = {}
    for r in rows:
        cid = str(r.get("condition_id") or "")
        if not cid:
            continue
        g = groups.setdefault(cid, {"buys": [], "exits": [], "max_ts": 0, "slug": "", "outcome": ""})
        rtype = str(r.get("type") or "trade")
        try:
            ts = int(r.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        g["max_ts"] = max(g["max_ts"], ts)
        if r.get("slug"):
            g["slug"] = r["slug"]
        if r.get("outcome"):
            g["outcome"] = r["outcome"]
        side = str(r.get("side") or "").upper()
        if rtype == "trade" and side == "BUY":
            g["buys"].append(r)
        elif rtype == "redeem" or (rtype == "trade" and side == "SELL"):
            g["exits"].append(r)

    records = []
    for cid, g in groups.items():
        if not g["buys"] or not g["exits"]:
            continue  # 未平仓（进行中窗口/纯兑付）不构成交易记录
        size = sum(float(b.get("size") or 0) for b in g["buys"])
        cost = sum(_amount(b) for b in g["buys"])
        income = sum(_amount(e) for e in g["exits"])
        if size <= 0 or cost <= 0:
            continue
        has_sell = any(str(e.get("side") or "").upper() == "SELL" for e in g["exits"])
        window_start = _window_from_slug(g["slug"])
        records.append(TradeRecord(
            ts=datetime.fromtimestamp(g["max_ts"], tz=timezone.utc).isoformat(timespec="seconds"),
            window_start=window_start,
            symbol=_symbol_from_slug(g["slug"]),
            direction=str(g["outcome"] or "").lower(),
            entry_price=round(cost / size, 6),
            exit_price=round(income / size, 6),
            size=round(size, 6),
            pnl=round(income - cost, 6),
            reason="sell" if has_sell else "settle",
        ))
    records.sort(key=lambda r: r.ts)
    return records


def _amount(row: dict) -> float:
    """流水金额：usdc_size（含手续费）优先，缺回退 size×price。"""
    try:
        usdc = float(row.get("usdc_size"))
        if usdc or row.get("usdc_size") is not None:
            return usdc
    except (TypeError, ValueError):
        pass
    try:
        return float(row.get("size") or 0) * float(row.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _window_from_slug(slug: str) -> int:
    """slug → 窗口起点秒（解析失败返回 0，流水配对按窗口 0 丢弃语义）。"""
    return window_start_from_slug(slug) or 0


def _symbol_from_slug(slug: str) -> str:
    return symbol_from_slug(slug)
