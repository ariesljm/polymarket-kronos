"""高频盘口采样：Polymarket WebSocket 市场频道 + REST 兜底。

- 订阅 wss://ws-subscriptions-clob.polymarket.com/ws/market（v2 官方 Market Channel）
- 订阅时 initial_dump 全深度快照（event_type=book，bids/asks 格式与 REST book 兼容）
- price_change 增量更新（side=BUY→bids，SELL→asks，size=0 删档）
- 断线指数退避重连，期间保留旧快照并按 interval 秒走 REST 兜底（并行拉取）
- subscribe() 动态增删订阅（update 消息，不重连）
- 主循环 tick 读内存快照（零网络等待）
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from websockets.asyncio.client import ClientConnection

from pmbot.book_price import weighted_price
from pmbot.ws_thread import ReconnectingWsThread

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _apply_price_change(snap: dict, change: dict) -> None:
    """price_change 增量更新快照：side=BUY→bids，SELL→asks，size=0 删档。"""
    side = "bids" if change.get("side") == "BUY" else "asks"
    price = change.get("price")
    size = change.get("size")
    if price is None:
        return
    levels = [l for l in snap[side] if l["price"] != price]
    if size and float(size) > 0:
        levels.append({"price": str(price), "size": str(size)})
    snap[side] = sorted(levels, key=lambda l: float(l["price"]), reverse=(side == "bids"))


def _subscription_diff(wanted: set[str], subbed: set[str]) -> tuple[list, list]:
    """计算订阅差异：(需要新增, 需要退订)。"""
    return [t for t in wanted if t not in subbed], [t for t in subbed if t not in wanted]


class BookSampler(ReconnectingWsThread):
    """后台线程跑 asyncio 事件循环：WS 订阅 → 内存盘口快照。"""

    ws_url = WS_URL

    def __init__(self, fetch_book: Callable[[str], dict] | None = None, interval: float = 2.0,
                 ws_url: str = WS_URL,
                 proxy: str | None = None, book_path: str | None = None,
                 book_flush_sec: float = 1.0):
        """fetch_book(token_id) -> book dict：WS 断线时的 REST 兜底（可为 None）。

        book_path: 落盘文件（data/book.json），供监控面板 1s 级实时盘口。
        """
        super().__init__(name="book-sampler", proxy=proxy)
        self.ws_url = ws_url
        self._fetch = fetch_book
        self._interval = interval
        # 断线等待期间 REST 兜底的轮询间隔（interval 参数真正生效；上限 2 秒防过频）
        self.disconnect_poll_sec = min(interval, 2.0)
        self.ws_url = ws_url
        self._proxy = proxy
        self._tokens: set[str] = set()
        self._snapshots: dict[str, dict] = {}
        self._direction_map: dict[str, str] = {}  # token_id -> up/down
        self._last_subscribed: set[str] = set()  # 已发送给服务端的订阅集合（WS 线程读写）
        self._book_path = Path(book_path) if book_path else None
        self._book_flush_sec = book_flush_sec
        self._lock = threading.Lock()

    # ---- 主循环接口（线程安全） ----

    def subscribe(self, tokens: list[str], direction_map: dict[str, str] | None = None) -> None:
        """更新订阅集合（窗口切换时调用）；退订的 token 清空快照。

        direction_map: {token_id: "up"/"down"}，用于 book.json 落盘方向标注。
        """
        wanted = set(tokens)
        changed = False
        with self._lock:
            changed = wanted != self._tokens
            for t in set(self._snapshots) - wanted:
                self._snapshots.pop(t, None)
            self._tokens = wanted
            if direction_map is not None:
                self._direction_map = dict(direction_map)
        if changed:
            # WS 连接中：动态推送官方 updateSubscription 消息（不重连）
            self._push_update()

    def snapshot(self, token_id: str) -> dict | None:
        """最近一次盘口快照（线程安全，dict 拷贝）。"""
        with self._lock:
            snap = self._snapshots.get(token_id)
            return dict(snap) if snap else None

    def _push_update(self) -> None:
        """订阅集合变化且 WS 连接中：推送官方 update 消息动态增删，避免等重连。"""
        ws = self._connected_ws
        loop = self._loop
        if ws is None or loop is None:
            return  # 未连接：重连时 _send_subscribe 全量订阅

        async def _do() -> None:
            with self._lock:
                wanted = set(self._tokens)
            add, rm = _subscription_diff(wanted, set(self._last_subscribed))
            if not add and not rm:
                return
            try:
                if rm:
                    await ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": rm}))
                if add:
                    await ws.send(json.dumps({"operation": "subscribe", "assets_ids": add}))
                self._last_subscribed = wanted
            except Exception:
                pass  # 连接已断开：重连后 _send_subscribe 全量订阅

        try:
            asyncio.run_coroutine_threadsafe(_do(), loop)
        except RuntimeError:
            pass  # loop 关闭（线程退出中）

    # ---- book.json 落盘（监控面板 1s 级实时盘口） ----

    def _flush_book(self) -> None:
        """按 direction_map 组装加权盘口价写 book.json；单方向独立计算。

        任一方向缺快照/流动性不足时该方向写 null、**文件照写且时间戳照更**
        （防止旧价冻结成面板上的"滞后"假象——面板对 null 显示 —）。
        """
        if self._book_path is None:
            return
        with self._lock:
            dm = dict(self._direction_map)
            snaps = {t: self._snapshots.get(t) for t in dm}
        if not dm:  # 尚未订阅任何方向：不落盘
            return
        prices: dict = {"ts": int(time.time() * 1000)}
        for token, label in dm.items():
            snap = snaps.get(token)
            if snap is None:
                prices[f"{label}_ask"] = None
                prices[f"{label}_bid"] = None
                continue
            ask = weighted_price(snap, "asks")
            bid = weighted_price(snap, "bids")
            prices[f"{label}_ask"] = round(ask, 6) if ask is not None else None
            prices[f"{label}_bid"] = round(bid, 6) if bid is not None else None
        try:
            self._book_path.write_text(json.dumps(prices), encoding="utf-8")
        except Exception:
            logger.warning("book.json 落盘失败", exc_info=True)

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._book_flush_sec):
            try:
                self._flush_book()
            except Exception:
                logger.exception("盘口落盘异常")

    def _start_flush(self) -> None:
        if self._book_path is None:
            return
        threading.Thread(target=self._flush_loop, daemon=True, name="book-flush").start()

    # ---- WS 客户端 ----

    def run(self) -> None:
        self._start_flush()
        super().run()

    def _on_disconnect(self) -> None:
        self._rest_fallback()

    def _while_disconnected(self) -> None:
        # 重连等待期间每 2 秒 REST 刷新快照，面板盘口不因 WS 断开而陈旧
        self._rest_fallback()

    async def _send_subscribe(self, ws: ClientConnection) -> None:
        with self._lock:
            tokens = list(self._tokens)
        if tokens:
            # 官方 market channel 订阅格式：type=market + assets_ids。
            # initial_dump/level 为非法字段：曾导致 1008 policy violation 拒收，
            # 盘口流全程靠 REST 兜底 + 无限重连刷屏（连接后服务端本就主动 dump 盘口）。
            await ws.send(json.dumps({
                "type": "market", "assets_ids": tokens,
            }))
            # 连接时全量订阅 → 更新已发送集合基线（后续增量 diff 的基础）
            self._last_subscribed = set(tokens)

    def _handle_message(self, raw: str) -> None:
        if raw == "PONG":
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(data, list):  # initial_dump: [book, ...]
            for item in data:
                self._apply_book_event(item)
            return
        etype = data.get("event_type")
        if etype == "book":
            self._apply_book_event(data)
        elif etype == "price_change":
            self._apply_price_changes(data.get("price_changes") or [])
        # last_trade_price / best_bid_ask 等事件不影响盘口快照，忽略

    def _apply_book_event(self, item: dict) -> None:
        asset_id = item.get("asset_id")
        if not asset_id:
            return
        snap = {
            "bids": [{"price": str(b["price"]), "size": str(b["size"])} for b in item.get("bids") or []],
            "asks": [{"price": str(a["price"]), "size": str(a["size"])} for a in item.get("asks") or []],
        }
        with self._lock:
            self._snapshots[asset_id] = snap

    def _apply_price_changes(self, changes: list) -> None:
        with self._lock:
            for c in changes:
                asset_id = c.get("asset_id")
                if not asset_id:
                    continue
                snap = self._snapshots.get(asset_id)
                if snap is None:
                    continue
                _apply_price_change(snap, c)

    def _rest_fallback(self) -> None:
        """WS 断开/重连等待期间：用 REST 并行刷新订阅中的快照（失败保留旧快照）。"""
        if self._fetch is None:
            return
        with self._lock:
            tokens = list(self._tokens)
        if not tokens:
            return
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(len(tokens), 4)) as pool:
            futures = {pool.submit(self._fetch, t): t for t in tokens}
            for fut in futures:
                tok = futures[fut]
                try:
                    book = fut.result()
                except Exception:
                    logger.warning("盘口 REST 兜底失败 token=%s", tok[:16] if tok else tok)
                    continue
                if book:
                    with self._lock:
                        self._snapshots[tok] = book
