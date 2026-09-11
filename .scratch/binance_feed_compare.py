"""Binance 价格流对比：miniTicker(1s 快照) vs aggTrade(实时逐笔)。

量化"穿越检测延迟"的可改善空间：推送间隔越小，越早发现穿越。
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time

BASE = "wss://data-stream.binance.vision/ws/btcusdt@{}"
PROXY = "http://127.0.0.1:10808"
DURATION = 45.0


async def sample(stream: str) -> None:
    import websockets

    url = BASE.format(stream)
    gaps: list[float] = []
    n = 0
    t0 = time.monotonic()
    last = None
    async with websockets.connect(url, proxy=PROXY, open_timeout=10) as ws:
        while time.monotonic() - t0 < DURATION:
            remaining = DURATION - (time.monotonic() - t0)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            now = time.monotonic()
            if last is not None:
                gaps.append(now - last)
            last = now
            n += 1
    med = statistics.median(gaps) * 1000 if gaps else 0
    p90 = sorted(gaps)[int(len(gaps) * 0.9)] * 1000 if gaps else 0
    print(f"{stream:12s} 消息={n:>6} ({n / DURATION:6.1f}/s)  "
          f"间隔中位={med:7.1f}ms  p90={p90:7.1f}ms  最大={max(gaps) * 1000:7.1f}ms")


async def main() -> None:
    print(f"对比 {DURATION:.0f}s（btcusdt）")
    for s in ("miniTicker", "aggTrade"):
        await sample(s)


if __name__ == "__main__":
    asyncio.run(main())
