"""决定性对照:requests(HTTP/1.1) vs httpx(HTTP/2),直连 vs 代理,对同一 clob 端点。

问题:book REST 偶发失败，到底是"路径(直连/代理)"问题还是"客户端库"问题？
轮转采样 4 组各 N 次，公平对照失败率。
"""
from __future__ import annotations

import json
import sys
import time

import requests

PROXY = "http://127.0.0.1:10808"
TIMEOUT = 5.0
N = 25


def current_token() -> str:
    now = int(time.time())
    last = None
    for attempt in range(4):
        for px in ({"https": PROXY}, {"http": None, "https": None}):
            try:
                r = requests.get("https://gamma-api.polymarket.com/markets",
                                 params={"slug": f"btc-updown-5m-{now // 300 * 300}"},
                                 timeout=8, proxies=px)
                return json.loads(r.json()[0]["clobTokenIds"])[0]
            except Exception as e:
                last = e
    raise SystemExit(f"取 token 失败: {last}")


def via_requests(url: str, proxy: dict | None) -> tuple[bool, float]:
    t0 = time.time()
    try:
        r = requests.get(url, timeout=TIMEOUT, proxies=proxy,
                         headers={"User-Agent": "pmbot/1.0"})
        r.raise_for_status()
        return True, time.time() - t0
    except Exception:
        return False, time.time() - t0


def via_httpx(url: str, proxy: str | None) -> tuple[bool, float]:
    import httpx

    t0 = time.time()
    try:
        with httpx.Client(http2=True, timeout=TIMEOUT, proxy=proxy) as c:
            r = c.get(url, headers={"User-Agent": "pmbot/1.0"})
        r.raise_for_status()
        return True, time.time() - t0
    except Exception:
        return False, time.time() - t0


def main() -> None:
    tok = current_token()
    url = f"https://clob.polymarket.com/book?token_id={tok}"
    direct = {"http": None, "https": None}
    proxy = {"http": PROXY, "https": PROXY}
    groups = [
        ("requests-直连", lambda: via_requests(url, direct)),
        ("requests-代理", lambda: via_requests(url, proxy)),
        ("httpx-直连", lambda: via_httpx(url, None)),
        ("httpx-代理", lambda: via_httpx(url, PROXY)),
    ]
    ok = {g[0]: 0 for g in groups}
    fails = {g[0]: [] for g in groups}
    times = {g[0]: [] for g in groups}
    total = time.time()
    for rnd in range(N):
        for name, fn in groups:
            good, dt = fn()
            if good:
                ok[name] += 1
                times[name].append(dt)
            else:
                fails[name].append(dt)
        print(f"轮 {rnd + 1:>2}/{N}  " + "  ".join(
            f"{n}={'ok' if ok[n] > 0 or not fails[n] else 'X'}" for n, _ in groups))
    print(f"\n耗时 {time.time()-total:.0f}s, 每组 {N} 次")
    for name, _ in groups:
        nf = len(fails[name])
        avg = sum(times[name]) / len(times[name]) if times[name] else 0
        print(f"  {name:14s} 成功 {ok[name]:>2}/{N}  失败 {nf:>2}  avg(成功) {avg:.2f}s"
              + (f"  失败耗时 {[f'{x:.1f}' for x in fails[name]]}" if nf else ""))


if __name__ == "__main__":
    main()
