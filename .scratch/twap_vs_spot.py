"""对比两套穿越口径:Binance 即时价 vs 60s TWAP(PM 结算口径)。

PM 规则(官方 changelog):5m crypto up/down 市场的「price to beat」与结算价
都取自 Chainlink 60s TWAP。本脚本用 Binance 1s K 线复现 60s TWAP,
量化「按即时价判定穿越」与「按 TWAP 判定穿越」的分歧率。

用法: uv run python .scratch/twap_vs_spot.py <回溯小时数>
"""
import collections
import sys
import time

from pmbot.data_source import fetch_klines_batch

SYMS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
THRESHOLD = 0.08          # 穿越阈值 %
TWAP_WIN = 60             # Chainlink 60s lookback
WINDOW = 300              # 5m 窗口


def load_1s(sym: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """分页拉 1s K 线 → {秒级 unix: close}。"""
    out: dict[int, float] = {}
    cur = start_ms
    while cur < end_ms:
        batch = fetch_klines_batch(sym, "1s", since=cur, limit=1000, proxies=None)
        if not batch:
            break
        for k in batch:
            ts = int(k.timestamp) // 1000
            out[ts] = float(k.close)
        nxt = int(batch[-1].timestamp) + 1000
        if nxt <= cur:
            break
        cur = nxt
    return out


def first_cross(series: list[tuple[int, float]], base: float) -> tuple[int, float] | None:
    """首次 |dev| >= 阈值 → (时刻, dev)。"""
    for t, p in series:
        dev = (p - base) / base * 100
        if abs(dev) >= THRESHOLD:
            return t, dev
    return None


def main(hours: float) -> None:
    now = int(time.time())
    start = (now - int(hours * 3600)) // WINDOW * WINDOW - 120   # 提前 120s 供 TWAP 预热
    print(f"拉取 {hours}h 的 1s K 线（{time.strftime('%H:%M', time.localtime(start))} ~ "
          f"{time.strftime('%H:%M', time.localtime(now))}）...", flush=True)
    spot: dict[str, dict[int, float]] = {}
    for s in SYMS:
        spot[s] = load_1s(s, start * 1000, now * 1000)
        print(f"  {s}: {len(spot[s])} 个 1s 点", flush=True)

    # 对齐时间轴
    common = sorted(set.intersection(*[set(v) for v in spot.values()]))
    idx = {t: i for i, t in enumerate(common)}
    print(f"  公共时间点: {len(common)}\n", flush=True)

    # 前缀和：60s 滚动平均 O(1) 查询（common 按秒对齐）
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
        if n < TWAP_WIN * 0.8:
            return None
        return (pref[s][i + 1] - pref[s][j]) / n

    stats = collections.Counter()
    diffs = []
    detail = []
    w_begin = ((start + TWAP_WIN) // WINDOW + 1) * WINDOW   # 对齐到 5m 边界
    for w0 in range(w_begin, now - WINDOW, WINDOW):
        for s in SYMS:
            i0 = idx.get(w0)
            if i0 is None:
                continue
            win = [i for i in range(i0, i0 + WINDOW) if i < len(common)]
            if len(win) < WINDOW * 0.8:
                continue
            base_spot = spot[s][w0]
            base_twap = twap60(s, i0)
            if base_twap is None:
                continue

            ser_spot = [(common[i], spot[s][common[i]]) for i in win]
            ser_twap = []
            for i in win:
                tw = twap60(s, i)
                if tw is not None:
                    ser_twap.append((common[i], tw))

            cs = first_cross(ser_spot, base_spot)
            ct = first_cross(ser_twap, base_twap)
            if cs and ct:
                if (cs[1] > 0) == (ct[1] > 0):
                    stats["both_same_dir"] += 1
                    diffs.append(cs[0] - ct[0])
                else:
                    stats["dir_conflict"] += 1     # 方向相反：我们做反了
                    detail.append((w0, s, "方向相反", cs[1], ct[1]))
            elif cs and not ct:
                stats["us_only"] += 1              # 我们穿越、PM 口径没穿越 → 假穿越
                detail.append((w0, s, "仅我们有(假穿越)", cs[1], None))
            elif ct and not cs:
                stats["pm_only"] += 1              # PM 穿越、我们没察觉 → 漏
                detail.append((w0, s, "仅PM有(漏)", None, ct[1]))
            else:
                stats["neither"] += 1

    total = sum(stats.values())
    print("=== 按 5m 窗口 × 标的 统计 ===")
    for k, v in stats.most_common():
        print(f"  {k:<16} {v:>5}  ({v/total*100:5.1f}%)")
    print(f"  {'合计':<16} {total:>5}")
    if diffs:
        ds = sorted(diffs)
        print(f"\n=== 两者都穿越且同向时，首次穿越时刻差（我们 − PM，秒）===")
        print(f"  中位 {ds[len(ds)//2]:+d}s  均值 {sum(ds)/len(ds):+.1f}s  "
              f"min {ds[0]:+d}s  max {ds[-1]:+d}s")
        print(f"  我们更早: {sum(1 for d in ds if d < 0)}   我们更晚: {sum(1 for d in ds if d > 0)}   同时: {sum(1 for d in ds if d == 0)}")
    print(f"\n=== 分歧样例（最多 12 条）===")
    for w0, s, kind, dv1, dv2 in detail[:12]:
        a = f"{dv1:+.3f}%" if dv1 is not None else "  —   "
        b = f"{dv2:+.3f}%" if dv2 is not None else "  —   "
        print(f"  {time.strftime('%H:%M', time.localtime(w0))} {s:<5} {kind:<16} 我们={a} PM={b}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 3)
