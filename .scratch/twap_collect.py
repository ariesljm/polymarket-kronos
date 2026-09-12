"""采集 Chainlink TWAP-60s(RTDS) + Binance 即时价，用于对比两套穿越口径。

RTDS 无历史/无重放(官方文档明示),只能实时采集,故本脚本需持续运行。
用法: uv run python .scratch/twap_collect.py <分钟数> <输出csv>
"""
import asyncio
import csv
import json
import sys
import time

import requests
import websockets

RTDS = "wss://ws-live-data.polymarket.com"
PROXY = "http://127.0.0.1:10808"
PAIRS = {
    "btc/usd": "BTCUSDT", "eth/usd": "ETHUSDT", "sol/usd": "SOLUSDT",
    "xrp/usd": "XRPUSDT", "doge/usd": "DOGEUSDT", "bnb/usd": "BNBUSDT",
}
NO_PROXY = {"http": None, "https": None}
SHORTS = [s.split("/")[0] for s in PAIRS]


def binance_all() -> dict:
    """一次请求取全部符号（不带 symbol 参数返回全量），避免 6 次往返。"""
    r = requests.get("https://data-api.binance.vision/api/v3/ticker/price",
                     timeout=6, proxies=NO_PROXY)
    keep = set(PAIRS.values())
    return {x["symbol"]: float(x["price"]) for x in r.json() if x["symbol"] in keep}


def write_row(w, f, twap, next_sample):
    now = time.time()
    try:
        b = binance_all()
    except Exception:
        b = {}
    w.writerow([f"{now:.3f}", time.strftime("%H:%M:%S", time.localtime(now))]
               + [f"{twap[s]:.10g}" if s in twap else "" for s in PAIRS]
               + [f"{b.get(PAIRS[s], '')}" for s in PAIRS])
    f.flush()
    return int(now) + 1


async def main(minutes: float, out_path: str) -> None:
    twap: dict[str, float] = {}
    stop_at = time.time() + minutes * 60
    next_sample = 0.0
    rows = 0
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["unix", "iso"] + [f"twap_{s}" for s in SHORTS]
                   + [f"bin_{s}" for s in SHORTS])
        f.flush()
        while time.time() < stop_at:
            try:
                async with websockets.connect(RTDS, open_timeout=40, proxy=PROXY) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                        {"topic": "crypto_prices_twap_sixty", "type": "update"}]}))
                    print(f"[{time.strftime('%H:%M:%S')}] 已连接并订阅", flush=True)
                    last_ping = time.time()
                    while time.time() < stop_at:
                        if time.time() - last_ping >= 5:      # 官方要求：每 5s PING
                            await ws.send("PING")
                            last_ping = time.time()
                        try:
                            # 0.05s 短超时：TWAP 推送约 8 条/秒，长超时会把消息积压在
                            # 队列里导致采样用的是陈旧值
                            m = await asyncio.wait_for(ws.recv(), timeout=0.05)
                        except asyncio.TimeoutError:
                            m = None
                        if m and m.strip() != "PONG":
                            try:
                                p = (json.loads(m).get("payload") or {})
                                if p.get("symbol") in PAIRS:
                                    twap[p["symbol"]] = float(p["value"])
                            except Exception:
                                pass
                        if time.time() >= next_sample:
                            next_sample = await asyncio.to_thread(write_row, w, f, dict(twap), next_sample)
                            rows += 1
                            if rows % 120 == 0:
                                print(f"[{time.strftime('%H:%M:%S')}] {rows} 行", flush=True)
            except Exception as e:
                print(f"重连({type(e).__name__}: {str(e)[:80]})", flush=True)
                await asyncio.sleep(2)
    print(f"完成：{rows} 行 → {out_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]), sys.argv[2]))
