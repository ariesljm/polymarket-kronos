"""高频盘口采样：Polymarket WebSocket 市场频道 + REST 兜底。

- 订阅 wss://ws-subscriptions-clob.polymarket.com/ws/market（v2 官方 Market Channel）
- 订阅时 initial_dump 全深度快照（event_type=book，bids/asks 格式与 REST book 兼容）
- price_change 增量更新（side=BUY→bids，SELL→asks，size=0 删档）
- 断线指数退避重连，期间保留旧快照并按 interval 秒走 REST 兜底（并行拉取）
- subscribe() 动态增删订阅（update 消息，不重连）
- 主循环 tick 读内存快照（零网络等待）
"""

from __future__ import annotations

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

# 快照新鲜度阈值（秒）：快照年龄超过此值视为陈旧（断线/订阅失效/事件流停摆），
# 消费方（best_ask/best_bid）与健康检查线程都会触发 REST 现拉刷新。
# WS 正常时 price_change 秒级到达，age 远小于此值；1s 收紧后止盈/止损决策价
# 最坏 ~1.5s 新鲜（REST 兜底 1s），避免 10s tick + 陈旧快照叠加的漏触发。
STALE_AGE_SEC = 1.0

# REST 兜底失败退避（秒）：指数增长，上限 RETRY_MAX。市场已结算/无订单簿（404）
# 与网络抖动都收敛到低频重试，避免断线/空转期间每秒刷屏（见 _rest_fallback）。
RETRY_BASE = 1.0
RETRY_MAX = 30.0


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


class BookSampler(ReconnectingWsThread):
    """后台线程跑 asyncio 事件循环：WS 订阅 → 内存盘口快照。"""

    ws_url = WS_URL

    def __init__(self, fetch_book: Callable[[str], dict] | None = None, interval: float = 2.0,
                 ws_url: str = WS_URL,
                 proxy: str | None = None, book_path: str | None = None,
                 book_flush_sec: float = 1.0, health_check_sec: float = 2.0) -> None:
        """fetch_book(token_id) -> book dict：WS 断线时的 REST 兜底（可为 None）。

        book_path: 落盘文件（data/book.json），供监控面板 1s 级实时盘口。
        health_check_sec: 健康检查间隔（秒）——WS 连接中快照缺失/陈旧的 token
        主动 REST 刷新（堵'连接活着但事件流停摆/订阅失效'盲区）。
        """
        super().__init__(name="book-sampler", proxy=proxy)
        self.ws_url = ws_url
        self._fetch = fetch_book
        self._interval = interval
        # 断线等待期间 REST 兜底的轮询间隔（interval 参数真正生效；上限 1 秒防过频）。
        # 实证：本环境代理隧道对 WS 高流量下行在 ~3-5s 内必断（见诊断），WS 可用率低，
        # REST 兜底是决策价的时效主力，1s 一轮把最坏年龄压在 ~1.5s。
        self.disconnect_poll_sec = min(interval, 1.0)
        self._tokens: set[str] = set()
        self._snapshots: dict[str, dict] = {}
        # 每个 token 快照的最后更新时间（monotonic 秒；与 _snapshots 同锁保护）——
        # 消费方据此判定陈旧，不再无条件信任不知来源年龄的快照
        self._snapshot_ts: dict[str, float] = {}
        # REST 兜底失败退避（同锁保护）：token -> 下次可重试时间 / 当前退避间隔（秒）
        self._retry_after: dict[str, float] = {}
        self._retry_backoff: dict[str, float] = {}
        self._direction_map: dict[str, str] = {}  # token_id -> up/down
        self._book_path = Path(book_path) if book_path else None
        self._book_flush_sec = book_flush_sec
        self._health_check_sec = health_check_sec
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
            for t in self._tokens - wanted:
                self._snapshots.pop(t, None)
                self._retry_after.pop(t, None)
                self._retry_backoff.pop(t, None)
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

    def snapshot_age(self, token_id: str) -> float | None:
        """快照年龄（秒，monotonic）。None=无快照；手动注入/旧数据无时间戳视为 0（新鲜）。"""
        with self._lock:
            if token_id not in self._snapshots:
                return None
            ts = self._snapshot_ts.get(token_id)
            return 0.0 if ts is None else time.monotonic() - ts

    def is_fresh(self, token_id: str) -> bool:
        """快照是否新鲜（陈旧判定单一事实源，消费方不再各自实现）。

        无快照 → 陈旧；手动注入/旧数据（无时间戳）→ 新鲜；年龄 ≤ STALE_AGE_SEC → 新鲜。
        BookSampler 健康检查与 ClobExecutor 决策价共用同一判定。
        """
        with self._lock:
            return not self._is_stale_locked(token_id)

    def update_snapshot(self, token_id: str, book: dict) -> None:
        """消费方 REST 现拉结果回填（线程安全）：更新快照并刷新时间戳。

        回填即节流——下个 tick 读到的快照年龄 < STALE_AGE_SEC，不再重复 REST。
        """
        with self._lock:
            self._snapshots[token_id] = book
            self._snapshot_ts[token_id] = time.monotonic()

    def _push_update(self) -> None:
        """订阅集合变化且 WS 连接中：推送官方 update 消息动态增删，避免等重连。"""
        with self._lock:
            wanted = set(self._tokens)
        self._push_subscriptions(wanted, "assets_ids")

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
            # ADR-0003：所有 JSON 状态文件原子写（monitor 进程并发读，防半写脏读）
            from pmbot.fileio import atomic_write_text

            atomic_write_text(self._book_path, json.dumps(prices))
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

    # ---- 健康检查（B：堵"连接活着但事件流停摆/订阅失效"盲区） ----

    def _is_stale_locked(self, token_id: str) -> bool:
        """快照缺失或年龄超过 STALE_AGE_SEC 判定为陈旧（调用方需持锁）。"""
        if token_id not in self._snapshots:
            return True
        ts = self._snapshot_ts.get(token_id)
        if ts is None:
            return False  # 手动注入/旧数据：视为新鲜，不主动拉
        return time.monotonic() - ts > STALE_AGE_SEC

    def _health_check(self) -> None:
        """WS 连接中：对快照缺失/陈旧的订阅 token 主动 REST 刷新。

        断线期间跳过（_while_disconnected 已有 2s REST 兜底，避免重复查询）；
        健康检查覆盖的是 WS 显示连接但事件流不推/动态订阅失败的场景。
        """
        if self._connected_ws is None:
            return
        if self._fetch is None:
            return
        with self._lock:
            tokens = list(self._tokens)
            stale = [t for t in tokens if self._is_stale_locked(t)]
        if not stale:
            return
        self._rest_fallback(tokens=stale)

    def _health_loop(self) -> None:
        while not self._stop.wait(self._health_check_sec):
            try:
                self._health_check()
            except Exception:
                logger.exception("盘口健康检查异常")

    def _start_health(self) -> None:
        threading.Thread(target=self._health_loop, daemon=True, name="book-health").start()

    # ---- WS 客户端 ----

    def run(self) -> None:
        self._start_flush()
        self._start_health()
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
            # 连接时全量订阅 → 基类 _mark_subscribed 记录基线（后续增量 diff 基础）
            self._mark_subscribed(tokens)

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
        elif etype in ("last_trade_price", "best_bid_ask"):
            # 轻量事件（D）：不重建整本快照，只做事件流心跳（刷新快照新鲜度）
            # + 记录轻量价。字段名服务端格式容错，解析失败只损失心跳。
            self._apply_light_event(data)

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
            self._snapshot_ts[asset_id] = time.monotonic()

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
                # price_change 每条自带服务端权威 top-of-book（官方文档字段）；
                # 存快照供 best_ask/best_bid 优先读——比本地档位排序更及时权威，零额外流量。
                if c.get("best_bid") is not None:
                    snap["best_bid"] = str(c["best_bid"])
                if c.get("best_ask") is not None:
                    snap["best_ask"] = str(c["best_ask"])
                self._snapshot_ts[asset_id] = time.monotonic()

    def best_ask(self, token_id: str) -> float | None:
        """最优卖价：优先 WS 权威 best_ask（price_change 携带），缺失则档位加权。"""
        snap = self.snapshot(token_id)
        if snap is None:
            return None
        if snap.get("best_ask") is not None:
            try:
                return float(snap["best_ask"])
            except (TypeError, ValueError):
                pass
        from pmbot.book_price import weighted_price

        return weighted_price(snap, "asks", size=1.0)

    def best_bid(self, token_id: str) -> float | None:
        """最优买价：优先 WS 权威 best_bid（price_change 携带），缺失则档位加权。"""
        snap = self.snapshot(token_id)
        if snap is None:
            return None
        if snap.get("best_bid") is not None:
            try:
                return float(snap["best_bid"])
            except (TypeError, ValueError):
                pass
        from pmbot.book_price import weighted_price

        return weighted_price(snap, "bids", size=1.0)

    def _apply_light_event(self, data: dict) -> None:
        """轻量行情事件（best_bid_ask / last_trade_price）：仅作事件流心跳（_touch 刷新新鲜度）。"""
        asset_id = data.get("asset_id") or data.get("asset")
        if not asset_id:
            return
        self._touch(asset_id)

    def _touch(self, asset_id: str) -> None:
        """刷新 token 的新鲜度时间戳（轻量事件证明事件流仍活着）。"""
        with self._lock:
            self._snapshot_ts[asset_id] = time.monotonic()

    def _rest_fallback(self, tokens: list | None = None) -> None:
        """REST 并行刷新快照（失败保留旧快照 + 按 token 指数退避）。

        断线/重连等待期间刷新全部订阅（tokens=None）；健康检查只刷陈旧的。
        写回同时更新新鲜度时间戳——REST 结果同样被消费方信任（时效内）。
        失败 token 进入指数退避（RETRY_BASE → 翻倍 → RETRY_MAX），成功清零：
        - 网络抖动：短暂重试即可恢复；
        - 市场已结算/无订单簿（404）：退避到上限后低频重试，不再每秒刷屏。
        """
        if self._fetch is None:
            return
        if tokens is None:
            with self._lock:
                tokens = list(self._tokens)
        if not tokens:
            return
        now = time.monotonic()
        with self._lock:
            pending = [t for t in tokens if now >= self._retry_after.get(t, 0.0)]
        if not pending:
            return
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(len(pending), 4)) as pool:
            futures = {pool.submit(self._fetch, t): t for t in pending}
            for fut in futures:
                tok = futures[fut]
                try:
                    book = fut.result()
                except Exception:
                    with self._lock:
                        backoff = self._retry_backoff.get(tok, 0.0) or RETRY_BASE
                        self._retry_after[tok] = now + backoff
                        self._retry_backoff[tok] = min(backoff * 2, RETRY_MAX)
                    logger.warning(
                        "盘口 REST 兜底失败 token=%s（%.0fs 后重试）",
                        tok[:16] if tok else tok, backoff,
                    )
                    continue
                if book:
                    with self._lock:
                        self._snapshots[tok] = book
                        self._snapshot_ts[tok] = now
                        # 成功清零退避（下次再失败从 RETRY_BASE 重新开始）
                        self._retry_after.pop(tok, None)
                        self._retry_backoff.pop(tok, None)
