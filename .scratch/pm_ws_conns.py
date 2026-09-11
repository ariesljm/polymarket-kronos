"""PM WS 断连根因实验:连接数是否压垮代理隧道。

场景 A:1 条连接订阅全部 12 个 token（合并架构）
场景 B:6 条连接各订阅 2 个 token（当前 bot 架构：每 symbol 一 BookSampler）
各跑 DURATION 秒，统计断开次数与原因。
"""
from __future__ import annotations

import asyncio
import json
import time

import requests

URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PROXY = "http://127.0.0.1:10808"
DURATION = 90.0
SYMS = ["btc", "eth", "sol", "xrp", "doge", "bnb"]


def tokens_by_symbol() -> dict[str, list[str]]:
    now = int(time.time())
    out = {}
    for sym in SYMS:
        for ws in (now // 300 * 300, now // 300 * 300 + 300):
            for px in ({"https": PROXY}, {"http": None, "https": None}):
                try:
                    r = requests.get("https://gamma-api.polymarket.com/markets",
                                     params={"slug": f"{sym}-updown-5m-{ws}"},
                                     timeout=8, proxies=px)
                    d = r.json()
                    if d and d[0].get("clobTokenIds"):
                        out[sym] = json.loads(d[0]["clobTokenIds"])
                        break
                except Exception:
                    continue
            if sym in out:
                break
    return out


async def one_conn(label: str, tokens: list[str], stats: dict, duration: float) -> None:
    import websockets

    t0 = time.time()
    while time.time() - t0 < duration:
        try:
            async with websockets.connect(URL, proxy=PROXY, open_timeout=10) as ws:
                await ws.send(json.dumps({"type": "market", "assets_ids": tokens}))
                while time.time() - t0 < duration:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=25)
                        stats[label]["msgs"] += 1
                    except asyncio.TimeoutError:
                        stats[label]["idle25"] += 1
                        break
        except Exception as e:
            stats[label]["disc"] += 1
            kind = f"{type(e).__name__}"
            stats[label]["kinds"][kind] = stats[label]["kinds"].get(kind, 0) + 1
            await asyncio.sleep(1)


async def scenario(name: str, groups: list[tuple[str, list[str]]], duration: float) -> None:
    stats: dict[str, dict] = {lbl: {"msgs": 0, "disc": 0, "idle25": 0, "kinds": {}}
                              for lbl, _ in groups}
    await asyncio.gather(*(one_conn(lbl, tk, stats, duration) for lbl, tk in groups))
    print(f"\n=== 场景 {name}: {len(groups)} 条连接, {duration:.0f}s ===")
    for lbl, _ in groups:
        s = stats[lbl]
        print(f"  {lbl:10s} 消息={s['msgs']:>5}  断开={s['disc']}  25s无数据={s['idle25']}  {s['kinds']}")


async def main() -> None:
    tb = tokens_by_symbol()
    print(f"取到 {len(tb)} 个 symbol 的 token: {list(tb)}")
    if len(tb) < 6:
        print("警告: token 不全，结果参考性下降")
    allt = [t for v in tb.values() for t in v]
    first2 = list(tb.values())[0]
    # 三场景：定位是“连接数”还是“时间偏差”（交替跑，降时间敏感）
    for rnd in range(2):
        await scenario(f"[轮{rnd+1}] 1连接×2token", [("solo", first2)], DURATION)
        await scenario(f"[轮{rnd+1}] 1连接×12token", [("merged", allt)], DURATION)
        await scenario(f"[轮{rnd+1}] 6连接×2token",
                       [(f"conn-{s}", tb[s]) for s in tb], DURATION)


if __name__ == "__main__":
    asyncio.run(main())
