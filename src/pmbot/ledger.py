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
from pathlib import Path

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

    判据唯一：api_trades.csv（API 真实流水配对，含手续费）存在则优先，
    否则回退 trades.csv（引擎业务记录）；两者都不存在返回 []。
    """
    data_dir = Path(data_dir)
    api = data_dir / "api_trades.csv"
    if api.is_file():
        from pmbot.trade_history import build_records

        with open(api, encoding="utf-8") as f:
            return build_records(list(csv.DictReader(f)))
    trades = data_dir / "trades.csv"
    if trades.is_file():
        return records_from_csv(trades)
    return []
