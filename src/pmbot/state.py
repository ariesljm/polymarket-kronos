"""交易状态：持仓、挂单、窗口、熔断计数、盈亏结算。

单标的单窗口一个 TradeState；盈亏与熔断计数在此维护，
status.json 持久化运行快照，trades.csv 记录每笔交易供统计。
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from pmbot.types import Direction, PendingOrder, Position, Signal

TRADE_COLUMNS = [
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


@dataclass
class TradeState:
    symbol: str
    window_start: int | None = None
    window_bet_placed: bool = False
    signal: Signal | None = None
    position: Position | None = None
    pending_order: PendingOrder | None = None
    consecutive_losses: int = 0
    daily_loss: float = 0.0
    paused: bool = False
    was_paused: bool = False
    pause_reason: str | None = None
    last_day: str = ""
    last_predict_sec: int | None = None
    predicting: bool = False
    predict_start_sec: int | None = None
    market_prices: dict | None = None

    def roll_window(self, window_start: int) -> None:
        """窗口切换：重置本窗口下注标记与挂单（持仓不应跨窗口，结算兜底）。"""
        if window_start == self.window_start:
            return
        self.window_start = window_start
        self.window_bet_placed = False
        self.pending_order = None
        self.signal = None

    def roll_day(self, day: str) -> None:
        """跨天重置当日亏损。"""
        if day != self.last_day:
            self.daily_loss = 0.0
            self.last_day = day

    def close_position(self, settle_price: float) -> float:
        """按结算/平仓价兑现持仓，返回盈亏；更新熔断计数。"""
        pos = self.position
        if pos is None:
            return 0.0
        pnl = pos.size * (settle_price - pos.entry_price)
        self.position = None
        if pnl < 0:
            self.consecutive_losses += 1
            self.daily_loss += -pnl  # 当日累计亏损（正数），与 decide 熔断语义一致
        else:
            self.consecutive_losses = 0  # 连续亏损在盈利后重置
        return pnl


class StateStore:
    """TradeState 持久化：status.json 快照 + trades.csv 交易日志。

    领域状态（TradeState）与存储分离：状态对象只维护交易语义，
    快照/日志的序列化细节收敛在此模块。
    """

    def __init__(self, status_path: str | Path = "data/status.json", trades_path: str | Path = "data/trades.csv"):
        self.status_path = Path(status_path)
        self.trades_path = Path(trades_path)

    # ---- 快照 ----

    def save(self, state: TradeState) -> None:
        data = asdict(state)
        if state.signal is not None:
            data["signal"] = {
                "direction": state.signal.direction.value,
                "p_up": state.signal.p_up,
            }
        # JSON 不支持注释：用 _comment 字段说明关键操作（load 时忽略未知字段）
        data["_comment"] = (
            "本文件由机器人自动维护，请勿手工修改非 _comment 字段。"
            "暂停/熔断后恢复：把 paused 改为 false 并重启 start_bot.bat。"
            "字段含义：paused=熔断暂停；daily_loss=当日累计亏损；"
            "consecutive_losses=连亏笔数；window_bet_placed=当前窗口已下注；"
            "signal=最近一次 Kronos 信号；position=当前持仓（无持仓为 null）。"
        )
        self.status_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def load(self) -> TradeState | None:
        if not self.status_path.is_file():
            return None
        try:
            data = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return None
        if "symbol" not in data:
            return None
        if data.get("position"):
            data["position"] = Position(
                direction=Direction(data["position"]["direction"]),
                entry_price=float(data["position"]["entry_price"]),
                size=float(data["position"]["size"]),
                entered_remaining_sec=int(data["position"]["entered_remaining_sec"]),
                window_start=int(data["position"]["window_start"]),
            )
        if data.get("pending_order"):
            po = data["pending_order"]
            try:
                data["pending_order"] = PendingOrder(
                    direction=Direction(po["direction"]),
                    price=float(po["price"]),
                    size=float(po.get("size", 0.0)),
                    order_id=str(po.get("order_id", "")),
                    created_sec=int(po.get("created_sec", 0)),
                )
            except (KeyError, ValueError, TypeError):
                data["pending_order"] = None
        if data.get("signal"):
            from pmbot.types import Signal as _S

            data["signal"] = _S(
                direction=Direction(data["signal"]["direction"]),
                p_up=float(data["signal"]["p_up"]),
            )
        return TradeState(**{k: data[k] for k in TradeState.__dataclass_fields__ if k in data})

    # ---- 交易日志 ----

    def log_trade(
        self,
        state: TradeState,
        *,
        window_start: int,
        direction: Direction,
        entry_price: float,
        exit_price: float,
        size: float,
        pnl: float,
        reason: str,
    ) -> None:
        p = self.trades_path
        new_file = not p.is_file()
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window_start": window_start,
            "symbol": state.symbol,
            "direction": direction.value,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "size": size,
            "pnl": round(pnl, 6),
            "reason": reason,
        }
        with open(p, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
