"""Binance 实时价后台线程：WS 实时成交流 + REST 兜底。

用途：决策引擎的「方向一致性过滤」数据源——窗口起点至今的实时移动
（live_delta_pct）只在信号方向与实时走势大幅矛盾时跳过入场，
不替代盘口价（止盈/止损仍用 Polymarket 盘口 bid）。

- WS：wss://data-stream.binance.vision/ws/<symbol>@aggTrade（主站
  stream.binance.com 大陆不可达 HTTP 451，用与 data-api.binance.vision
  配套的数据流镜像；实测直连可达）
  —— 用 aggTrade（实时逐笔成交）而非 miniTicker（固定 1s 快照）：
  momentum 的入场价完全取决于穿越发现得多快（盘口穿越后秒级极化），
  固定 1s 粒度 = 最多 1s 后才看到穿越，足以错过未调价的便宜档。
- REST 兜底：镜像 ticker/price，断线重连等待期间 1s 轮询（与 BookSampler
  同模式——本环境 WS 稳定性差，兜底是时效主力）
- 消费方：TradingLoop.build_view 计算 live_delta_pct（线程安全读内存）。

Binance WS 心跳为帧级 ping（websockets 库协议层自动应答），无应用层
文本 PING，与 ReconnectingWsThread 的 Polymarket 心跳应答不冲突。
"""

from __future__ import annotations

import json
import logging
from typing import Callable
import threading
import time

from pmbot.data_source import normalize_symbol
from pmbot.ws_thread import ReconnectingWsThread

logger = logging.getLogger(__name__)

# Binance 数据流镜像 WS（与 K 线 REST 镜像同源；连接自带流声明，无需订阅消息）
# WS stream 路径用小写交易对（btcusdt@aggTrade）；REST 端点用大写（镜像 400 拒小写）
# 组合流 aggTrade + miniTicker：穿越检测延迟是入场价的核心变量，单用 aggTrade 时
# 成交稀疏的标的推送间隔反而从 miniTicker 的固定 1s 退化到 1.6-5.3s（SOL/XRP/
# DOGE/BNB 实测中位），穿越最多晚 5s；两者并存则活跃标的拿逐笔、稀疏标的拿 1s。
WS_URL_TMPL = (
    "wss://data-stream.binance.vision/stream"
    "?streams={sym}@aggTrade/{sym}@miniTicker"
)
# REST 兜底（断线等待期间 1s 轮询；强制直连不跟随代理——data_source 同款实证）
REST_URL_TMPL = "https://data-api.binance.vision/api/v3/ticker/price?symbol={sym}"
REST_POLL_SEC = 1.0


class SpotTickerThread(ReconnectingWsThread):
    """Binance 实时价线程：WS miniTicker → 内存最新价；断线 REST 兜底。"""

    disconnect_poll_sec = REST_POLL_SEC
    # Binance 单流 WS 无应用层 PING 心跳（协议层 ping 由 websockets 库自动
    # 处理）；发应用层 PING 文本无益且可能被当作未知消息，禁用 _ping_loop。
    app_heartbeat_sec = None

    def __init__(self, symbol: str = "BTC", *, proxy: str | None = None,
                 fetch_ticker: Callable[[], float | None] | None = None,
                 on_update: Callable[[], None] | None = None) -> None:
        super().__init__(name="spot-ticker", proxy=proxy)
        self.symbol = normalize_symbol(symbol)
        self.ws_url = WS_URL_TMPL.format(sym=self.symbol.lower())
        self._rest_url = REST_URL_TMPL.format(sym=self.symbol)
        self._fetch = fetch_ticker or self._rest_fetch  # 测试注入点（同 BookSampler）
        self._lock = threading.Lock()
        self._price: float | None = None
        self._delta: float = 0.0  # 面板展示：最近一次价格差（与 SpotPrice 语义一致）
        self._ts: float = 0.0
        # 价格更新事件通知（事件驱动入场：WS 推送到达即触发主循环 tick，
        # 减少轮询相位延迟——MM 秒级极化，0-1s 的发现延迟 = 入场价系统性变差）
        self._on_update: Callable[[], None] | None = on_update
        self._last_notify = 0.0

    def set_on_update(self, cb: Callable[[], None]) -> None:
        """设置价格更新回调（线程安全：仅写引用；主循环创建后注入）。"""
        self._on_update = cb

    # ---- 消费方接口（线程安全） ----

    def latest_price(self) -> float | None:
        """最近一次 Binance 实时价（线程安全）。尚无成功拉取返回 None。"""
        with self._lock:
            return self._price

    def snapshot(self) -> dict | None:
        """价格快照（面板顶栏用，与 SpotPrice.snapshot 兼容）：{"price", "delta"}。"""
        with self._lock:
            if self._price is None:
                return None
            return {"price": self._price, "delta": self._delta}

    # ---- WS 钩子（ReconnectingWsThread 子类实现） ----

    async def _send_subscribe(self, ws: "ClientConnection") -> None:
        # 流已在 URL 查询串声明，连上即收，无需发送订阅消息
        pass

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        # 组合流（/stream?streams=）把负载包在 "data" 下，单流不包
        if "stream" in data and isinstance(data.get("data"), dict):
            data = data["data"]
        # aggTrade 实时成交价在 "p"；miniTicker（事件名 24hrMiniTicker）收盘价在 "c"
        # （两者语义同为 last trade price，仅推送频率不同）
        price_raw = data.get("p") if data.get("e") == "aggTrade" else data.get("c")
        if price_raw is None:
            return
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            return
        self._update(price)

    def _update(self, price: float) -> None:
        """记录最新价与涨跌差（WS/REST 共用；首次无 delta）。"""
        with self._lock:
            if self._price is not None:
                self._delta = price - self._price
            self._price = price
            self._ts = time.monotonic()
        # 通知回调（锁外调用：回调可能重入 latest_price 取价，避免自锁死锁）；
        # 0.15s 最小间隔节流——WS 1s 推送 + REST 兜底 1s 常态 ≤2 次/s，防突发风暴
        cb = self._on_update
        if cb is not None:
            now = time.monotonic()
            if now - self._last_notify >= 0.15:
                self._last_notify = now
                try:
                    cb()
                except Exception:
                    logger.exception("%s 更新回调异常", self.__class__.__name__)

    def _fallback_once(self) -> None:
        """断线 + 重连等待期间 REST 兜底（1s 轮询，保持价格新鲜）。"""
        self._rest_fallback()

    def _rest_fallback(self) -> None:
        price = self._fetch()
        if price is None:
            return  # 失败静默保留旧值（下次轮询重试）
        self._update(price)

    def _rest_fetch(self) -> float | None:
        """REST 兜底默认实现：Binance 公共镜像直连（不跟随环境代理，K 线源同款实证）。"""
        import requests

        try:
            r = requests.get(self._rest_url, timeout=5,
                             proxies={"http": None, "https": None})
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            return None