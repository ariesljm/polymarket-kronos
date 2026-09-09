"""WS 稳定性对照探测:直连 vs 代理,针对 Binance 与 Polymarket 两个独立上游。

用法:
  python ws_probe.py <binance|polymarket> <direct|proxy> [秒数]

direct = 显式不走代理(不传 proxy 参数,不读环境变量);
proxy  = 显式走 http://127.0.0.1:10808。
输出:连接建立耗时、存活时长、收到消息数、断开原因。
"""
import asyncio
import json
import os
import sys
import time

import websockets

BINANCE = "wss://data-stream.binance.vision/ws/btcusdt@miniTicker"
POLYMARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PROXY = "http://127.0.0.1:10808"
# BTC 5m 当前窗口 Up/Down token(探测用)
BTC_TOKENS = [
    "24891337658406200559516052224612004446379078362590320635225859188424020653815",
    "7490677339349519911110975057511849165022790879169623197468138553455070243198",
]


async def run(url: str, proxy: str | None, duration: float, label: str) -> None:
    kw = {"open_timeout": 10}
    if proxy:
        kw["proxy"] = proxy
    t_start = time.monotonic()
    try:
        async with websockets.connect(url, **kw) as ws:
            t_conn = time.monotonic() - t_start
            n = 0
            ping_task = None
            if "polymarket" in url:
                # Polymarket 需订阅 assets_ids 才能收盘口流(否则 1008);发订阅后再发 PING 保持
                await ws.send(json.dumps({"type": "market", "assets_ids": BTC_TOKENS}))

                async def pinger():
                    while True:
                        await asyncio.sleep(9)
                        await ws.send("PING")

                ping_task = asyncio.create_task(pinger())
            try:
                while time.monotonic() - t_start < duration:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=5)
                        n += 1
                    except asyncio.TimeoutError:
                        pass
                alive = time.monotonic() - t_start
                print(f"[{label}] 稳定 {alive:.1f}s | 连接耗时 {t_conn:.2f}s | 收 {n} 条消息")
            except Exception as e:
                alive = time.monotonic() - t_start
                print(f"[{label}] 中途断开 {alive:.1f}s 处 | 连接耗时 {t_conn:.2f}s | 已收 {n} 条 | {type(e).__name__}: {e}")
            finally:
                if ping_task:
                    ping_task.cancel()
    except Exception as e:
        alive = time.monotonic() - t_start
        print(f"[{label}] 连接失败 {alive:.1f}s 处 | {type(e).__name__}: {e}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "binance"
    mode = sys.argv[2] if len(sys.argv) > 2 else "direct"
    dur = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
    url = POLYMARKET if target == "polymarket" else BINANCE
    if mode == "direct":
        # 清除环境代理变量:websockets 的 proxy=None 会回退读环境变量,不清则"直连"实为走代理
        for k in list(os.environ):
            if "proxy" in k.lower():
                os.environ.pop(k, None)
        proxy = None
    else:
        proxy = PROXY
    label = f"{target}/{mode}"
    asyncio.run(run(url, proxy, dur, label))
