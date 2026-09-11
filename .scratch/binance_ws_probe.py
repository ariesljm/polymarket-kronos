"""Binance WS 直连 vs 代理 稳定性对照(各连 60s)。

背景:SpotTickerThread 的 WS 走代理(v2rayN),但其 REST 兜底走直连
(spot_ticker._rest_fetch 注释:"Binance 直连可达,经代理反而超时")。
本脚本量化两种方式下 Binance WS 的实际断开行为,验证"WS 是否也该直连"。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

URL = "wss://data-stream.binance.vision/ws/btcusdt@miniTicker"
PROXY = "http://127.0.0.1:10808"
DURATION = 60.0


async def probe(label: str, proxy: str | None) -> None:
    import websockets

    t0 = time.time()
    msgs = 0
    disconnects = 0
    last_err = "-"
    while time.time() - t0 < DURATION:
        try:
            async with websockets.connect(URL, proxy=proxy, open_timeout=10) as ws:
                while time.time() - t0 < DURATION:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=DURATION)
                        msgs += 1
                    except asyncio.TimeoutError:
                        break
        except Exception as e:
            disconnects += 1
            last_err = f"{type(e).__name__}: {str(e)[:60]}"
            await asyncio.sleep(1)
    dur = time.time() - t0
    print(f"{label:8s} {dur:.0f}s  消息={msgs:>5}  断开次数={disconnects}  最近错误={last_err}")


async def main() -> None:
    await probe("直连", None)
    await probe("代理", PROXY)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
