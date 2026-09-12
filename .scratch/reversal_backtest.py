"""回测：穿越后「买反向」（赌弱信号回摆）的胜率分布 —— 反向策略第一步验证。

信号/结算口径与 momentum 策略完全一致：
- 信号：Binance 1s 价相对窗口开盘 dev ≥ 0.08% 视为穿越（首次突破即穿越）
- 结算：PM 规则 = TWAP-60s 窗口末 vs 窗口初（Chainlink 口径，用 Binance 1s 均值复现）

输出: 按「穿越后 60s 内峰值 |dev|」（信号强度）分档 —— 弱穿越是否更多回摆?
用法: uv run python .scratch/reversal_backtest.py [回溯小时数]
"""
import collections
import datetime
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from pmbot.data_source import fetch_klines_batch

SYMS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
THRESHOLD = 0.08
TWAP_WIN = 60
WINDOW = 300
BUCKETS = [(0.08, 0.12, "0.08-0.12"), (0.12, 0.16, "0.12-0.16"),
           (0.16, 0.22, "0.16-0.22"), (0.22, 0.30, "0.22-0.30"),
           (0.30, 1e9, "0.30+")]


def bucket(p: float) -> str:
    for lo, hi, name in BUCKETS:
        if lo <= p < hi:
            return name
    return "0.30+"


def load_1s(sym: str, start_s: int, end_s: int) -> dict[int, float]:
    out: dict[int, float] = {}
    cur = start_s * 1000
    while cur < end_s * 1000:
        batch = fetch_klines_batch(sym, "1s", since=cur, limit=1000, proxies=None)
        if not batch:
            break
        for k in batch:
            out[int(k.timestamp) // 1000] = float(k.close)
        nxt = int(batch[-1].timestamp) + 1000
        if nxt <= cur:
            break
        cur = nxt
    return out


def main(hours: float) -> None:
    now = int(time.time())
    start = int(((now - hours * 3600) // WINDOW) * WINDOW - 120)
    print(f"拉取 {hours}h 1s K 线 "
          f"({datetime.datetime.fromtimestamp(start).strftime('%m-%d %H:%M')} ~ "
          f"{datetime.datetime.fromtimestamp(now).strftime('%m-%d %H:%M')}) ...", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        spot = dict(zip(SYMS, ex.map(lambda s: load_1s(s, start, now), SYMS)))
    for s in SYMS:
        print(f"  {s}: {len(spot[s])} 点", flush=True)
    print(f"拉取耗时 {time.time()-t0:.0f}s", flush=True)

    common = sorted(set.intersection(*[set(v) for v in spot.values()]))
    idx = {t: i for i, t in enumerate(common)}
    pref: dict[str, list[float]] = {}
    for s in SYMS:
        acc = 0.0
        p = [0.0]
        for t in common:
            acc += spot[s][t]
            p.append(acc)
        pref[s] = p

    def twap60(s: str, i: int) -> float | None:
        j = max(0, i - TWAP_WIN + 1)
        n = i - j + 1
        return (pref[s][i + 1] - pref[s][j]) / n if n >= TWAP_WIN * 0.8 else None

    # 统计: (sym, w0) 窗口
    all_rows: list[dict] = []
    w_begin = ((start + TWAP_WIN) // WINDOW + 1) * WINDOW
    for w0 in range(w_begin, now - WINDOW, WINDOW):
        i0 = idx.get(w0)
        if i0 is None or i0 + WINDOW >= len(common):
            continue
        for s in SYMS:
            base = spot[s].get(w0)
            bt = twap60(s, i0)
            st = twap60(s, i0 + WINDOW)
            if base is None or bt is None or st is None:
                continue
            # 首次穿越
            cross_i = cross_dev = None
            for i in range(i0, min(i0 + WINDOW, len(common))):
                dev = (spot[s][common[i]] - base) / base * 100
                if abs(dev) >= THRESHOLD:
                    cross_i, cross_dev = i, dev
                    break
            if cross_i is None:
                continue
            cross_dir = "up" if cross_dev > 0 else "down"
            peak = max(
                abs((spot[s][common[i]] - base) / base * 100)
                for i in range(cross_i, min(cross_i + 60, len(common))))
            out_tw = "up" if st >= bt else "down"
            all_rows.append({"sym": s, "w0": w0, "dir": cross_dir,
                             "peak": peak, "fwd_win": out_tw == cross_dir,
                             "rev_win": out_tw != cross_dir})

    print(f"\n穿越窗口(窗口×标的): {len(all_rows)}\n", flush=True)
    if not all_rows:
        return

    def report(title: str, rows: list[dict]) -> None:
        print(f"=== {title} ===")
        print(f"{'峰值dev':>10} {'n':>5} {'顺向胜率':>9} {'反向胜率':>9} {'峰值中位':>9}")
        g = collections.defaultdict(list)
        for r in rows:
            g[bucket(r["peak"])].append(r)
        for b in [x[2] for x in BUCKETS]:
            v = g.get(b) or []
            if not v:
                continue
            n = len(v)
            fw = sum(1 for r in v if r["fwd_win"]) / n
            rw = sum(1 for r in v if r["rev_win"]) / n
            med = statistics.median(r["peak"] for r in v)
            print(f"{b:>10} {n:>5} {fw:>8.1%} {rw:>8.1%} {med:>9.3f}")
        n = len(rows)
        fw = sum(1 for r in rows if r["fwd_win"]) / n
        rw = sum(1 for r in rows if r["rev_win"]) / n
        print(f"{'全部':>10} {n:>5} {fw:>8.1%} {rw:>8.1%} "
              f"{statistics.median(r['peak'] for r in rows):>9.3f}\n")

    report("全部穿越窗口（按穿越后峰值 |dev| 分档）", all_rows)
    report("[up] 方向", [r for r in all_rows if r["dir"] == "up"])
    report("[down] 方向", [r for r in all_rows if r["dir"] == "down"])

    # 按标的稳定性
    print("=== 按标的（全部穿越） ===")
    print(f"{'标的':<6} {'n':>5} {'反向胜率':>9} {'峰中位':>8}")
    for s in SYMS:
        v = [r for r in all_rows if r["sym"] == s]
        if not v:
            continue
        rw = sum(1 for r in v if r["rev_win"]) / len(v)
        print(f"{s:<6} {len(v):>5} {rw:>8.1%} {statistics.median(r['peak'] for r in v):>8.3f}")
    # 按天稳定
    print(f"\n=== 按天（全部穿越） ===")
    byday = collections.defaultdict(list)
    for r in all_rows:
        byday[datetime.datetime.fromtimestamp(r["w0"]).strftime("%m-%d")].append(r)
    for d in sorted(byday):
        v = byday[d]
        rw = sum(1 for r in v if r["rev_win"]) / len(v)
        print(f"  {d}: n={len(v):>4} 反向胜率={rw:6.1%}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 43)