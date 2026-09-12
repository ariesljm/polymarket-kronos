"""User Channel：Polymarket 认证 WebSocket（订单/成交实时推送）。

- 订阅 wss://ws-subscriptions-clob.polymarket.com/ws/user
- auth 用 CLOB API 凭证（apiKey/secret/passphrase，与 REST 同源，缓存于 clob_creds.json）
- 事件（order/trade）入线程安全队列，主循环 tick drain 处理（无锁竞争）
- 每 10 秒 PING 保活；断线指数退避重连；markets 过滤可动态增删
"""

from __future__ import annotations

import json
import logging
import queue
import threading

from websockets.asyncio.client import ClientConnection

from pmbot.ws_thread import ReconnectingWsThread

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class UserStream(ReconnectingWsThread):
    """后台线程跑 asyncio 事件循环：认证 WS → 事件队列。"""

    ws_url = WS_URL
    # 事件流（只推订单/成交，稀疏）：空闲是正常的，不适用行情流的僵尸检测
    # （否则连接后在无事件推送时被判僵尸→无限重连循环）
    stale_idle_sec = None

    def __init__(self, auth: dict | None = None, proxy: str | None = None,
                 ws_url: str = WS_URL) -> None:
        """auth: {"apiKey", "secret", "passphrase"}；无 auth 时仅空转（不连接）。"""
        super().__init__(name="user-stream", proxy=proxy)
        self.ws_url = ws_url
        self._auth = auth
        self._markets: list[str] = []
        self._lock = threading.Lock()
        self._events: queue.Queue = queue.Queue()
        self.connected = False  # 线程安全读（bool 赋值原子）

    # ---- 主循环接口（线程安全） ----

    def subscribe_markets(self, condition_ids: list[str]) -> None:
        """更新 markets 过滤（窗口切换时调用）；增量同步到 WS。"""
        with self._lock:
            self._markets = list(condition_ids)
        self._push_update()

    def _push_update(self) -> None:
        """markets 变化且 WS 连接中：推送 operation 消息动态增删，避免等重连。"""
        with self._lock:
            wanted = set(self._markets)
        self._push_subscriptions(wanted, "markets")

    def drain(self) -> list[tuple[str, dict]]:
        """取出全部待处理事件（主循环 tick 调用）：[(event_type, payload), ...]"""
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # ---- WS 客户端 ----

    def run(self) -> None:
        if not self._auth:
            logger.info("无 CLOB 凭证，UserStream 不启动")
            return
        super().run()

    def _on_connect(self) -> None:
        super()._on_connect()  # 重置 _last_data_ts（见基类说明）
        self.connected = True
        logger.info("用户 WS 已连接（auth ok）")

    def _fallback_once(self) -> None:
        # 断线即置断开标记（幂等；重连成功后 _on_connect 恢复）。曾以
        # _on_disconnect 表达，基类双钩子收敛为单钩子后语义不变。
        self.connected = False

    async def _send_subscribe(self, ws: ClientConnection) -> None:
        with self._lock:
            markets = list(self._markets)
            self._mark_subscribed(markets)
        await ws.send(json.dumps({
            "auth": self._auth, "type": "user", "markets": markets,
        }))

    def _handle_message(self, raw: str) -> None:
        if raw == "PONG":
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        etype = data.get("event_type")
        if etype in ("order", "trade"):
            self._events.put((etype, data))
