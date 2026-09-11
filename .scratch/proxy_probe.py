"""直连 vs 代理 稳定性对比监测(默认 1 小时)。

不改动正在运行的 bot,独立采样 Polymarket REST 端点,直连与代理各测一次,
量化两种链路的成功率/耗时/失败类型,回答"直连是否比 v2rayN 代理更稳"。

采样项(对应 bot 实际链路):
  - clob /time          (requests HTTP/1.1  ← fetch_book / book_sampler REST 兜底)
  - clob /time          (httpx http2=True   ← py_clob_client_v2 下单/余额/结算)
  - gamma markets       (requests           ← market_discovery)
  - data-api positions  (requests           ← 钱包核对/结算兑付)
  - binance klines      (requests 直连优先   ← K 线,已知直连更稳的参考系)

判定:拿到 HTTP 响应(任意状态码)= 连通成功;抛超时/连接/SSL 异常 = 失败。
超时 8s(对齐 fetch_book 的 6s 上限附近,不卡整轮)。

输出:
  - CSV 每轮每项一行 → .scratch/proxy_probe/probe.csv
  - 结束时 stdout 汇总直连 vs 代理的成功率/平均耗时/P95/失败类型

用法:
  uv run python .scratch/proxy_probe.py            # 默认 60 分钟,每 60s 一轮
  uv run python .scratch/proxy_probe.py --minutes 2 --interval 10   # 快速自检
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path

PROXY = "http://127.0.0.1:10808"
TIMEOUT = 8.0
OUT = Path(".scratch/proxy_probe")
OUT.mkdir(parents=True, exist_ok=True)
CSV_PATH = OUT / "probe.csv"

ENDPOINTS = [
    ("clob_requests", "https://clob.polymarket.com/time"),
    ("clob_httpx_h2", "https://clob.polymarket.com/time"),
    ("gamma", "https://gamma-api.polymarket.com/markets?limit=1&active=true&closed=false"),
    ("data_api", "https://data-api.polymarket.com/positions?limit=1"),
    ("binance", "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=1"),
]


def probe_requests(url: str, proxy: bool) -> tuple[bool, float, str]:
    import requests

    t0 = time.time()
    try:
        r = requests.get(url, timeout=TIMEOUT, proxies={"https": PROXY} if proxy else None)
        # 任意状态码都算链路连通成功
        return True, time.time() - t0, f"HTTP {r.status_code}"
    except Exception as e:
        return False, time.time() - t0, type(e).__name__


def probe_httpx_h2(url: str, proxy: bool) -> tuple[bool, float, str]:
    import httpx

    t0 = time.time()
    try:
        with httpx.Client(http2=True, timeout=TIMEOUT,
                          proxy=PROXY if proxy else None) as c:
            r = c.get(url)
        return True, time.time() - t0, f"HTTP {r.status_code}"
    except Exception as e:
        return False, time.time() - t0, type(e).__name__


def probe(endpoint: str, url: str, proxy: bool) -> tuple[bool, float, str]:
    if endpoint == "clob_httpx_h2":
        return probe_httpx_h2(url, proxy)
    return probe_requests(url, proxy)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args()

    rounds = max(1, int(args.minutes * 60 / args.interval))
    print(f"监测开始: {rounds} 轮, 每 {args.interval:.0f}s 一轮, "
          f"代理={PROXY}, CSV={CSV_PATH}")

    header = ["ts", "endpoint", "mode", "ok", "latency_s", "detail"]
    new_file = not CSV_PATH.exists()
    f = open(CSV_PATH, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new_file:
        w.writerow(header)

    # 汇总: mode -> endpoint -> [latencies], failures
    lat: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    fails: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    fail_kind: dict[str, dict[str, defaultdict]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    try:
        for rnd in range(1, rounds + 1):
            ts = time.strftime("%H:%M:%S")
            line = []
            for ep, url in ENDPOINTS:
                for proxy, mode in ((True, "proxy"), (False, "direct")):
                    ok, sec, detail = probe(ep, url, proxy)
                    w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), ep, mode,
                                int(ok), f"{sec:.3f}", detail])
                    if ok:
                        lat[mode][ep].append(sec)
                    else:
                        fails[mode][ep] += 1
                        fail_kind[mode][ep][detail] += 1
                    line.append(f"{ep[:7]}/{mode[:1]}={'ok' if ok else 'X'}({sec:.1f}s)")
            f.flush()
            print(f"[{ts}] 第{rnd:>3}/{rounds}轮  " + "  ".join(line))
            if rnd < rounds:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n手动中断,输出当前汇总...")
    finally:
        f.close()

    print("\n" + "=" * 72)
    print(f"汇总(共采样 {rounds} 轮)")
    print("=" * 72)
    for mode in ("direct", "proxy"):
        print(f"\n【{mode}】")
        total_ok = total_fail = 0
        for ep, _ in ENDPOINTS:
            lats = lat[mode].get(ep, [])
            nf = fails[mode].get(ep, 0)
            n = len(lats) + nf
            total_ok += len(lats)
            total_fail += nf
            if lats:
                avg = statistics.mean(lats)
                p95 = sorted(lats)[int(len(lats) * 0.95) - 1]
                print(f"  {ep:14s} 成功 {len(lats):>3}/{n}  avg {avg:.2f}s  p95 {p95:.2f}s"
                      + (f"  失败: {dict(fail_kind[mode][ep])}" if nf else ""))
            else:
                print(f"  {ep:14s} 全失败 {n}  类型: {dict(fail_kind[mode][ep])}")
        print(f"  {'合计':14s} 成功 {total_ok}/{total_ok + total_fail}  失败 {total_fail}")


if __name__ == "__main__":
    main()
