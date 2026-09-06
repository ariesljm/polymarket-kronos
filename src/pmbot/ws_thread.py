"""可重连 WS 线程基类：后台线程跑 asyncio 事件循环。

统一连接生命周期骨架（指数退避重连 + 应用层心跳应答 + 优雅停止）
与订阅集合动态推送（_push_subscriptions：跨线程安全调度 + 增量 diff），
业务差异由子类实现：订阅消息、消息处理、连接/断线回调。

两个消费者（BookSampler / UserStream）共享此骨架，第三个 WS 流直接复用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time

from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

RECONNECT_BASE = 5.0  # 重连退避起步（秒）：行情数据新鲜度优先——断线 5s 内重试
RECONNECT_MAX = 60.0  # 重连退避上限（秒）
STALE_IDLE_SEC = 25.0  # 无数据僵尸连接检测：超过该时长无任何消息 → 主动重连


class ReconnectingWsThread(threading.Thread):
    """WS 连接线程骨架。子类实现四个钩子即可接入。"""

    ws_url: str = ""
    reconnect_base: float = RECONNECT_BASE
    reconnect_max: float = RECONNECT_MAX
    disconnect_poll_sec: float = 2.0  # 断线等待期间子类兜底钩子的轮询间隔（秒）
    # 应用层心跳间隔（秒）：Polymarket Market/User Channel 要求客户端每 10s
    # 主动发 PING（不发会被 ~10s 后断开）。None=禁用（Binance 单流靠协议层
    # ping，发应用层 PING 文本无益且可能被当作未知消息）。
    app_heartbeat_sec: float | None = 10.0

    def __init__(self, *, name: str | None = None, proxy: str | None = None) -> None:
        super().__init__(daemon=True, name=name or self.__class__.__name__)
        self._proxy = proxy
        self._stop = threading.Event()
        self.disconnect_poll_sec = self.__class__.disconnect_poll_sec
        # WS 线程写、主线程只读：当前连接与事件循环（动态订阅更新用）
        self._connected_ws: ClientConnection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # 最后收到数据时刻（僵尸连接检测：服务端不推数据但连接不关 → 主动重连）
        self._last_data_ts = 0.0
        # 已发送给服务端的订阅集合（基类持有，_push_subscriptions 增量 diff 基线；
        # 子类 _send_subscribe 经 _mark_subscribed 更新——不再各自手写初始化）
        self._last_subscribed: set[str] = set()

    # ---- 对外接口 ----

    def stop(self) -> None:
        self._stop.set()

    def connection_status(self) -> str:
        """当前连接状态（供面板/心跳展示；只读 Event 与引用，线程安全）。

        connected=WS 已建立 / reconnecting=断线退避重连中 / stopped=已停。
        """
        if self._stop.is_set():
            return "stopped"
        return "connected" if self._connected_ws is not None else "reconnecting"

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
                        # 应用层心跳：客户端每 N 秒主动发 PING（Polymarket
                        # Market/User Channel 官方规则：不发会被服务端 ~10s 后
                        # 断开）。旧实现误判“服务端主动发 PING/客户端只应答”，
                        # 实为当时频率/格式问题，现行规则要求 10s 间隔纯文本 PING。
                        # Binance 单流（app_heartbeat_sec=None）靠协议层 ping，不启用。
                        ping_task = (
                            asyncio.create_task(self._ping_loop(ws))
                            if self.app_heartbeat_sec else None
                        )
                        try:
                            async for msg in ws:
                                self._last_data_ts = time.monotonic()
                                if await self._answer_heartbeat(ws, msg):
                                    continue  # 心跳应答不交给子类
                                self._handle_message(msg)
                        finally:
                            if ping_task is not None:
                                ping_task.cancel()
                    finally:
                        self._connected_ws = None
                        self._loop = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("%s 断开（%s），%.0fs 后重连", self.__class__.__name__, e, backoff)
                self._on_disconnect()
                # 等待重连期间周期调用兜底钩子（如 REST 轮询），保持数据新鲜。
                # 重连等待加 ±50% 抖动：多 bot 并发断开时错开同步握手，
                # 避免同时重连触发服务端连接限流（曾见 3 bot 同步重连风暴）。
                wait_total = backoff * random.uniform(0.5, 1.5)
                waited = 0.0
                while not self._stop.is_set() and waited < wait_total:
                    self._while_disconnected()
                    wait = min(self.disconnect_poll_sec, wait_total - waited)
                    self._stop.wait(wait)
                    waited += wait
                backoff = min(backoff * 2, self.reconnect_max)

    async def _ping_loop(self, ws: ClientConnection) -> None:
        """应用层心跳：每 app_heartbeat_sec 秒主动发 PING（Polymarket 规则）。

        官方文档要求客户端每 10s 发 PING、服务端回 PONG；不发则 ~10s 后被断开
        （回归：盘口流连接后 ~10s 必断、反复重连刷屏）。PONG 由子类
        _handle_message 忽略（BookSampler/UserStream 均已处理）。连接已断时
        send 抛异常 → 静默退出，外层 _ws_loop 捕获异常重连。
        """
        interval = self.app_heartbeat_sec or 10.0
        try:
            while not self._stop.is_set():
                await asyncio.sleep(interval)
                # 僵尸连接检测：长时间无数据（服务端静默但连接未关）→ 主动断开触发重连
                if time.monotonic() - self._last_data_ts > STALE_IDLE_SEC:
                    logger.warning(
                        "%s 僵尸连接（%.0fs 无数据），主动重连",
                        self.__class__.__name__, time.monotonic() - self._last_data_ts,
                    )
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                try:
                    await ws.send("PING")
                except Exception:
                    return  # 连接已断，外层会重连
        except asyncio.CancelledError:
            return

    async def _answer_heartbeat(self, ws: ClientConnection, msg: object) -> bool:
        """应用层心跳应答（兼容）：服务端发 PING 文本 → 回 PONG。

        现行 Polymarket 规则以客户端主动发 PING 为心跳主路径（见 _ping_loop），
        服务端不再主动发 PING；但保留本应答作为双向兼容（万一服务端仍会发 PING
        要求回 PONG）。返回 True 表示已应答、消息不交子类。
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

    def _mark_subscribed(self, tokens) -> None:
        """记录已发送给服务端的订阅集合（_send_subscribe 连接时全量订阅后调用，
        供 _push_subscriptions 做增量 diff 基线）。"""
        self._last_subscribed = set(tokens)

    # ---- 订阅集合动态推送 ----

    def _push_subscriptions(self, wanted: set[str], payload_key: str,
                            on_error: Callable[[], None] | None = None) -> None:
        """订阅集合变化且 WS 连接中：推送 operation 消息动态增删，避免等重连。

        wanted: 当前想要的完整订阅集合；payload_key: 服务端键名（如 assets_ids/markets）。
        统一经 run_coroutine_threadsafe 跨线程调度（loop.create_task 从非事件循环线程
        调用不是线程安全的，曾致丢任务竞争窗口）。未连接时不推：重连时 _send_subscribe
        全量订阅。on_error: 发送失败回调（缺省静默——断线后全量重订阅兑底）。
        """
        ws = self._connected_ws
        loop = self._loop
        if ws is None or loop is None:
            return

        async def _do() -> None:
            subbed = set(self._last_subscribed)
            add = wanted - subbed
            rm = subbed - wanted
            if not add and not rm:
                return
            try:
                if rm:
                    await ws.send(json.dumps(
                        {"operation": "unsubscribe", payload_key: sorted(rm)}))
                if add:
                    await ws.send(json.dumps(
                        {"operation": "subscribe", payload_key: sorted(add)}))
                self._last_subscribed = set(wanted)
            except Exception:
                if on_error is not None:
                    on_error()
                else:
                    logger.warning("订阅增量同步失败", exc_info=True)

        try:
            asyncio.run_coroutine_threadsafe(_do(), loop)
        except RuntimeError:
            pass  # loop 关闭（线程退出中）
