"""Binance 实时价后台线程：WS miniTicker 推送 + REST 兜底。

用途：决策引擎的「方向一致性过滤」数据源——窗口起点至今的实时移动
（live_delta_pct）只在信号方向与实时走势大幅矛盾时跳过入场，
不替代盘口价（止盈/止损仍用 Polymarket 盘口 bid）。

- WS：wss://data-stream.binance.vision/ws/<symbol>@miniTicker（主站
  stream.binance.com 大陆不可达 HTTP 451，用与 data-api.binance.vision
  配套的数据流镜像；实测直连可达，~1s 推送一条）
- REST 兜底：镜像 ticker/price，断线重连等待期间 1s 轮询（与 BookSampler
  同模式——本环境 WS 稳定性差，兜底是时效主力）
- 消费方：TradingLoop.build_view 计算 live_delta_pct（线程安全读内存）。

Binance WS 心跳为帧级 ping（websockets 库协议层自动应答），无应用层
文本 PING，与 ReconnectingWsThread 的 Polymarket 心跳应答不冲突。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from pmbot.data_source import normalize_symbol
from pmbot.ws_thread import ReconnectingWsThread

logger = logging.getLogger(__name__)

# Binance 数据流镜像 WS（与 K 线 REST 镜像同源；单流 GET 连接，无需订阅消息）
# WS stream 路径用小写交易对（btcusdt@miniTicker）；REST 端点用大写（镜像 400 拒小写）
WS_URL_TMPL = "wss://data-stream.binance.vision/ws/{sym}@miniTicker"
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
                 fetch_ticker=None):
        sym = symbol.lower().replace("/", "")
        if not sym.endswith("usdt"):
            sym += "usdt"
        super().__init__(name="spot-ticker", proxy=proxy)
        self.symbol = normalize_symbol(symbol)
        self.ws_url = WS_URL_TMPL.format(sym=sym)
        self._rest_url = REST_URL_TMPL.format(sym=self.symbol)
        self._proxy = proxy  # REST 兜底也走环境代理（与调度一致）；WS 直连时传 None
        self._fetch = fetch_ticker or self._rest_fetch  # 测试注入点（同 BookSampler）
        self._lock = threading.Lock()
        self._price: float | None = None
        self._delta: float = 0.0  # 面板展示：最近一次价格差（与 SpotPrice 语义一致）
        self._ts: float = 0.0

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

    async def _send_subscribe(self, ws) -> None:
        # Binance 单流 GET 连接（/ws/<stream>）无需发送订阅消息，连上即收
        pass

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        close = data.get("c")
        if close is None:
            return
        try:
            price = float(close)
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

    def _while_disconnected(self) -> None:
        """等待重连期间 REST 兜底（1s 轮询，保持价格新鲜）。"""
        self._rest_fallback()

    def _on_disconnect(self) -> None:
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