"""可重连 WS 线程基类：后台线程跑 asyncio 事件循环。

统一连接生命周期骨架（指数退避重连 + 应用层心跳应答 + 优雅停止），
业务差异由子类实现：订阅消息、消息处理、连接/断线回调。

两个消费者（BookSampler / UserStream）共享此骨架，第三个 WS 流直接复用。
"""

from __future__ import annotations

import asyncio
import logging
import threading

from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

RECONNECT_BASE = 2.0
RECONNECT_MAX = 30.0


class ReconnectingWsThread(threading.Thread):
    """WS 连接线程骨架。子类实现四个钩子即可接入。"""

    ws_url: str = ""
    reconnect_base: float = RECONNECT_BASE
    reconnect_max: float = RECONNECT_MAX

    def __init__(self, *, name: str | None = None, proxy: str | None = None):
        super().__init__(daemon=True, name=name or self.__class__.__name__)
        self._proxy = proxy
        self._stop = threading.Event()
        # WS 线程写、主线程只读：当前连接与事件循环（动态订阅更新用）
        self._connected_ws: ClientConnection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 对外接口 ----

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            asyncio.run(self._ws_loop())
        except Exception:
            logger.exception("%s 线程异常退出", self.__class__.__name__)

    # ---- 连接生命周期 ----

    async def _ws_loop(self) -> None:
        import websockets

        backoff = self.reconnect_base
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.ws_url, open_timeout=15, proxy=self._proxy,
                ) as ws:
                    backoff = self.reconnect_base
                    self._on_connect()
                    logger.info("%s 已连接", self.__class__.__name__)
                    self._connected_ws = ws
                    self._loop = asyncio.get_running_loop()
                    try:
                        await self._send_subscribe(ws)
                        try:
                            async for msg in ws:
                                if await self._answer_heartbeat(ws, msg):
                                    continue  # 心跳应答不交给子类
                                self._handle_message(msg)
                        finally:
                            pass
                    finally:
                        self._connected_ws = None
                        self._loop = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("%s 断开（%s），%.0fs 后重连", self.__class__.__name__, e, backoff)
                self._on_disconnect()
                # 等待重连期间周期调用兜底钩子（如 REST 轮询），保持数据新鲜
                waited = 0.0
                while not self._stop.is_set() and waited < backoff:
                    self._while_disconnected()
                    wait = min(2.0, backoff - waited)
                    self._stop.wait(wait)
                    waited += wait
                backoff = min(backoff * 2, self.reconnect_max)

    async def _answer_heartbeat(self, ws: ClientConnection, msg) -> bool:
        """Polymarket 应用层心跳应答：服务端发 PING 文本 → 回 PONG（应答即保活）。

        客户端**不主动发** PING：曾每 3s 发 PING 文本被服务端判非法
        （1008 policy violation）→ WS 每 3s 断开重连（回归：20:51 盘口流
        连接后 ~3s 必断，REST 兜底救场）。返回 True 表示已应答、消息不交子类。
        """
        if isinstance(msg, str) and msg.strip() == "PING":
            try:
                await ws.send("PONG")
            except Exception:
                pass
            return True
        return False

    # ---- 子类钩子 ----

    def _on_connect(self) -> None:
        """连接成功回调（默认无操作）。"""

    def _on_disconnect(self) -> None:
        """断线回调（默认无操作；如 REST 兜底、状态标记）。"""

    def _while_disconnected(self) -> None:
        """等待重连期间的周期兜底钩子（默认无操作；如 REST 轮询保持数据新鲜）。"""

    async def _send_subscribe(self, ws: ClientConnection) -> None:
        """连接后发送订阅消息。"""

    def _handle_message(self, raw: str) -> None:
        """处理收到的消息。"""
