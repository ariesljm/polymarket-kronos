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

from pmbot.ws_thread import ReconnectingWsThread

logger = logging.getLogger(__name__)

# Binance 数据流镜像 WS（与 K 线 REST 镜像同源；单流 GET 连接，无需订阅消息）
WS_URL_TMPL = "wss://data-stream.binance.vision/ws/{sym}@miniTicker"
# REST 兜底（断线等待期间 1s 轮询；强制直连不跟随代理——data_source 同款实证）
REST_URL_TMPL = "https://data-api.binance.vision/api/v3/ticker/price?symbol={sym}"
REST_POLL_SEC = 1.0


def _binance_symbol(symbol: str) -> str:
    """规范化交易对：BTC → BTCUSDT（幂等：BTCUSDT 保持）。"""
    s = symbol.replace("/", "").upper()
    return s if s.endswith("USDT") else s + "USDT"


class SpotTickerThread(ReconnectingWsThread):
    """Binance 实时价线程：WS miniTicker → 内存最新价；断线 REST 兜底。"""

    disconnect_poll_sec = REST_POLL_SEC

    def __init__(self, symbol: str = "BTC", *, proxy: str | None = None,
                 fetch_ticker=None):
        sym = symbol.lower().replace("/", "")
        if not sym.endswith("usdt"):
            sym += "usdt"
        super().__init__(name="spot-ticker", proxy=proxy)
        self.symbol = _binance_symbol(symbol)
        self.ws_url = WS_URL_TMPL.format(sym=sym)
        self._rest_url = REST_URL_TMPL.format(sym=self.symbol)
        self._proxy = proxy  # REST 兜底也走环境代理（与调度一致）；WS 直连时传 None
        self._fetch = fetch_ticker or self._rest_fetch  # 测试注入点（同 BookSampler）
        self._lock = threading.Lock()
        self._price: float | None = None
        self._ts: float = 0.0

    # ---- 消费方接口（线程安全） ----

    def latest_price(self) -> float | None:
        """最近一次 Binance 实时价（线程安全）。尚无成功拉取返回 None。"""
        with self._lock:
            return self._price

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
        with self._lock:
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
        with self._lock:
            self._price = price
            self._ts = time.monotonic()

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