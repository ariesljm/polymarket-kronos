"""交易品种实时价轮询：Binance 公共数据镜像（与 K 线同源）。

从 monitor.py 提取的独立关注点：后台线程定时拉取现货价格，
失败静默保留旧值。面板/Web 控制台经 snapshot() 取最新价。
"""

from __future__ import annotations

import threading


class SpotPrice:
    """交易品种实时价轮询（Binance 公共数据镜像，与 K 线同源；失败静默保留旧值）。"""

    def __init__(self, symbol: str = "ETH", interval: float = 3.0):
        self.symbol = symbol
        self.interval = interval
        self._lock = threading.Lock()
        self._price: float | None = None
        self._delta: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="spot-price", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict | None:
        """当前价格快照；尚无成功拉取返回 None。"""
        with self._lock:
            if self._price is None:
                return None
            return {"price": self._price, "delta": self._delta}

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._tick()

    def _tick(self) -> None:
        import requests

        try:
            r = requests.get(
                f"https://data-api.binance.vision/api/v3/ticker/price?symbol={self.symbol}USDT",
                timeout=5,
            )
            r.raise_for_status()
            price = float(r.json()["price"])
        except Exception:
            return  # 网络失败：静默保留旧值
        with self._lock:
            if self._price is not None:
                self._delta = price - self._price
            self._price = price
