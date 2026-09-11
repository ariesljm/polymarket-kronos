"""Polymarket 盘口 WS 直连 vs 代理 稳定性对照(各连 60s)。

背景:BookSampler 的 PM WS 走代理(v2rayN),今日断开 618 次。本脚本量化
直连/代理两种方式下 PM market channel 的实际断开行为。

订阅格式(与 book_sampler._send_subscribe 一致):{"type":"market","assets_ids":[...]}
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import requests

URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PROXY = "http://127.0.0.1:10808"
DURATION = 60.0


def current_tokens() -> list[str]:
    now = int(time.time())
    for ws in (now // 300 * 300, now // 300 * 300 + 300):
        slug = f"btc-updown-5m-{ws}"
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"slug": slug}, timeout=10,
                         proxies={"https": PROXY})
        d = r.json()
        if d and d[0].get("clobTokenIds"):
            return json.loads(d[0]["clobTokenIds"])
    raise SystemExit("取不到当前窗口 token")


async def probe(label: str, proxy: str | None, tokens: list[str]) -> None:
    import websockets

    t0 = time.time()
    msgs = 0
    disconnects = 0
    last_err = "-"
    while time.time() - t0 < DURATION:
        try:
            async with websockets.connect(URL, proxy=proxy, open_timeout=10) as ws:
                await ws.send(json.dumps({"type": "market", "assets_ids": tokens}))
                while time.time() - t0 < DURATION:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=DURATION)
                        msgs += 1
                    except asyncio.TimeoutError:
                        break
        except Exception as e:
            disconnects += 1
            last_err = f"{type(e).__name__}: {str(e)[:70]}"
            await asyncio.sleep(1)
    print(f"{label:8s} {time.time()-t0:.0f}s  消息={msgs:>4}  断开次数={disconnects}  最近错误={last_err}")


async def main() -> None:
    tokens = current_tokens()
    print(f"订阅 {len(tokens)} 个 token: {tokens[0][:24]}...")
    await probe("直连", None, tokens)
    await probe("代理", PROXY, tokens)


if __name__ == "__main__":
    asyncio.run(main())
