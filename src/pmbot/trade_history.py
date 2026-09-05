"""Polymarket 交易历史同步：data-api 为事实源，本地增量缓存（api_trades.csv）。

与本地业务记录（trades.csv，引擎逐笔写入）并存：本模块维护的 api_trades.csv
是 API 真实流水（含手续费金额的 usdcSize、transactionHash），供对账/审计/
统计重建。增量更新 = 按 transactionHash 去重，只拉新增流水，不重复全量。

流水来源两条：
- /trades（type=trade）：成交记录（BUY/SELL）
- /activity?type=REDEEM（type=redeem）：结算兑付（usdcSize = 实际到账）

同步在后台线程（daemon）执行：不阻塞主循环 tick；网络失败静默跳过，
下一周期重试。fetch 函数可注入（测试用 fake）。
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from pmbot.ledger import RECORD_COLUMNS, TradeRecord
logger = logging.getLogger(__name__)


@runtime_checkable
class TradeHistorySource(Protocol):
    """流水同步所需的执行器窄接口（ClobExecutor 满足；SimExecutor 无）。"""

    def fetch_trade_page(self, offset: int = 0, limit: int = 500) -> list[dict]: ...
    def fetch_redeem_page(self, offset: int = 0, limit: int = 500) -> list[dict]: ...

COLUMNS = [
    "ts",
    "type",       # trade / redeem
    "side",       # BUY / SELL（redeem 为空）
    "size",
    "price",
    "usdc_size",  # trade: 含手续费金额；redeem: 实际到账
    "condition_id",
    "title",
    "slug",
    "outcome",
    "tx_hash",
]

PAGE_SIZE = 500
MAX_PAGES = 20

# 交易记录 schema 单一事实源在 ledger（引擎写入/展示/统计共用）
# （本模块不再手抄 RECORD_COLUMNS——曾与 ledger 同 schema 双份维护）

class TradeHistorySyncer:
    """增量同步 Polymarket 交易流水到本地 CSV（后台线程，节流轮询）。"""

    def __init__(
        self,
        path: str | Path,
        fetch_trades,
        fetch_redeems,
        poll_sec: int = 300,
    ):
        self.path = Path(path)
        self._fetch_trades = fetch_trades
        self._fetch_redeems = fetch_redeems
        self._poll_sec = poll_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 对外 ---- 

    def start(self) -> None:
        """启动后台同步线程（立即执行首次同步）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="trade-history-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def sync(self) -> int:
        """增量同步一次：拉取新增流水追加落盘，返回新增条数。

        去重键 = transactionHash（本地已有集合）；网络失败/无地址返回 0（静默）。
        """
        known = self._load_known_tx()
        new = []
        new += self._fetch_new(self._fetch_trades, known)
        new += self._fetch_new(self._fetch_redeems, known)
        if not new:
            return 0
        self._append(new)
        return len(new)

    # ---- 内部 ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = self.sync()
                if n:
                    logger.info("交易历史同步：新增 %d 条（%s）", n, self.path)
            except Exception:
                logger.exception("交易历史同步失败，下一周期重试")
            self._stop.wait(self._poll_sec)

    def _load_known_tx(self) -> set[str]:
        if not self.path.is_file():
            return set()
        known: set[str] = set()
        try:
            with open(self.path, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    tx = (row.get("tx_hash") or "").strip()
                    if tx:
                        known.add(tx)
        except Exception:
            logger.warning("读取 %s 失败，按空历史处理", self.path)
        return known

    def _fetch_new(self, fetch, known_tx: set[str]) -> list[dict]:
        """分页拉取流水直到历史区：本页全部已知（已同步过）或到尾页即停。"""
        rows: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            page = fetch(offset=offset, limit=PAGE_SIZE) or []
            if not page:
                break
            fresh = [t for t in page if str(t.get("transactionHash") or "") not in known_tx]
            rows.extend(fresh)
            if len(page) < PAGE_SIZE or not fresh:
                break  # 到尾页，或已到已同步的历史区
            offset += len(page)
        return rows

    def _append(self, rows: list[dict]) -> None:
        new_file = not self.path.is_file()
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            if new_file:
                writer.writeheader()
            for r in rows:
                writer.writerow({
                    "ts": r.get("timestamp"),
                    "type": "redeem" if r.get("type") == "REDEEM" else "trade",
                    "side": r.get("side") or "",
                    "size": r.get("size"),
                    "price": r.get("price"),
                    "usdc_size": r.get("usdcSize"),
                    "condition_id": r.get("conditionId"),
                    "title": r.get("title"),
                    "slug": r.get("slug"),
                    "outcome": r.get("outcome"),
                    "tx_hash": r.get("transactionHash"),
                })
