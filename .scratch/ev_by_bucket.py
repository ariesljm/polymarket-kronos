"""按 ask 档位统计真实 EV：市场定价 vs PM(TWAP-60s)口径的实际结算胜率。

PM 规则：5m crypto up/down 市场的「price to beat」与结算价都取自 Chainlink
60s TWAP。本脚本用 Binance 1s K 线复现该口径，判定每个窗口的真实结果，
再与日志里记录的真实盘口 ask 对比，找出哪个档位/方向存在正 EV。

入场记录来自 logs/multi.log 的 rej 行（含 sym/win/dir/ask），
取每个 (标的, 窗口) 的**首次**记录（最早被拒的那次 = 最接近穿越时刻的盘口）。

用法: uv run python .scratch/ev_by_bucket.py [回溯小时数]
"""
import collections
import datetime
import glob
import re
import statistics
import sys
import time

from pmbot.data_source import fetch_klines_batch

SYMS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
TWAP_WIN = 60
WINDOW = 300
LINE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*rej sym=(\w+) win=(\d+) dir=(\w+) ask=([\d.]+)")


def parse_rejections(hours: float) -> dict[tuple[str, int], tuple[str, float]]:
    """{(sym, win): (dir, first_ask)} —— 每窗口的首次盘口记录。"""
    cut = time.time() - hours * 3600
    first: dict[tuple[str, int], tuple[str, float]] = {}
    order: dict[tuple[str, int], float] = {}
    # 按天滚动的日志: logs/multi.log + logs/multi.log.YYYY-MM-DD
    files = sorted(glob.glob("logs/multi.log*"))
    for path in files:
        for ln in open(path, encoding="utf-8", errors="ignore"):
            m = LINE_RE.match(ln)
            if not m:
                continue
            ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            if ts < cut:
                continue
            sym, win, d, ask = m.group(2), int(m.group(3)), m.group(4), float(m.group(5))
            if sym not in SYMS or d not in ("up", "down"):
                continue
            k = (sym, win)
            if k not in first or ts < order[k]:
                first[k] = (d, ask)
                order[k] = ts
    return first


def twap60_at(series: dict[int, float], t: int) -> float | None:
    """时刻 t 的 60s TWAP（时间加权：对 1s 采样取均值）。"""
    pts = [series[x] for x in range(t - TWAP_WIN + 1, t + 1) if x in series]
    return sum(pts) / len(pts) if len(pts) >= TWAP_WIN * 0.8 else None


def main(hours: float) -> None:
    rej = parse_rejections(hours)
    print(f"日志（最近 {hours}h）里有首次盘口记录的 (标的,窗口): {len(rej)}", flush=True)
    wins = sorted({w for _, w in rej})
    if not wins:
        print("无记录"); return
    print(f"覆盖 {len(wins)} 个窗口: "
          f"{datetime.datetime.fromtimestamp(wins[0]).strftime('%m-%d %H:%M')} ~ "
          f"{datetime.datetime.fromtimestamp(wins[-1]).strftime('%m-%d %H:%M')}\n", flush=True)

    # 只拉有记录窗口的 1s K 线 [w0-60, w0+300]
    need: dict[str, list[int]] = collections.defaultdict(list)
    for sym, w in rej:
        need[sym].append(w)
    serie: dict[tuple[str, int], dict[int, float]] = {}
    for sym in SYMS:
        for i, w in enumerate(sorted(set(need[sym]))):
            if i % 20 == 0:
                print(f"  拉取 {sym} ... {i}/{len(set(need[sym]))}", flush=True)
            data: dict[int, float] = {}
            cur = (w - TWAP_WIN) * 1000
            end = (w + WINDOW) * 1000
            while cur < end:
                batch = fetch_klines_batch(sym, "1s", since=cur, limit=1000, proxies=None)
                if not batch:
                    break
                for k in batch:
                    data[int(k.timestamp) // 1000] = float(k.close)
                nxt = int(batch[-1].timestamp) + 1000
                if nxt <= cur:
                    break
                cur = nxt
            serie[(sym, w)] = data

    # 判定每个窗口的 PM 口径结果 + 即时口径结果
    rows = []
    for (sym, w), (d, ask) in rej.items():
        s = serie.get((sym, w)) or {}
        base_tw = twap60_at(s, w)
        settle_tw = twap60_at(s, w + WINDOW)
        base_sp = s.get(w)
        settle_sp = s.get(w + WINDOW)
        if None in (base_tw, settle_tw, base_sp, settle_sp):
            continue
        out_tw = "up" if settle_tw >= base_tw else "down"
        out_sp = "up" if settle_sp >= base_sp else "down"
        rows.append({
            "sym": sym, "win": w, "dir": d, "ask": ask,
            "out_tw": out_tw, "out_sp": out_sp,
            "win_tw": d == out_tw, "win_sp": d == out_sp,
        })
    print(f"\n成功判定 {len(rows)} 个样本\n")
    if not rows:
        return

    # 方向分布
    ups = sum(1 for r in rows if r["dir"] == "up")
    print(f"方向分布: up={ups} down={len(rows)-ups}")

    def bucket(a: float) -> str:
        lo = int(a * 20) / 20        # 0.05 一档
        return f"{lo:.2f}-{lo+0.05:.2f}"

    print("\n=== ① 按「我们方向的 ask」分档：PM(TWAP)口径 vs 即时口径 ===")
    print(f"{'档位':>10} {'n':>4} {'真实胜率(TWAP)':>15} {'真实胜率(即时)':>15} {'ask均值':>9} {'EV(TWAP)':>10} {'EV(即时)':>10}")
    g = collections.defaultdict(list)
    for r in rows:
        g[bucket(r["ask"])].append(r)
    for b in sorted(g):
        v = g[b]
        n = len(v)
        wr_tw = sum(1 for r in v if r["win_tw"]) / n
        wr_sp = sum(1 for r in v if r["win_sp"]) / n
        mean_ask = statistics.mean(r["ask"] for r in v)
        print(f"{b:>10} {n:>4} {wr_tw:>14.1%} {wr_sp:>14.1%} {mean_ask:>9.3f} "
              f"{wr_tw-mean_ask:>+10.3f} {wr_sp-mean_ask:>+10.3f}")

    # 整体
    n = len(rows)
    wr_tw = sum(1 for r in rows if r["win_tw"]) / n
    wr_sp = sum(1 for r in rows if r["win_sp"]) / n
    ma = statistics.mean(r["ask"] for r in rows)
    print(f"{'全部':>10} {n:>4} {wr_tw:>14.1%} {wr_sp:>14.1%} {ma:>9.3f} "
          f"{wr_tw-ma:>+10.3f} {wr_sp-ma:>+10.3f}")

    print("\n=== ② 若买入「反向（便宜侧）」：赔率 = 1 − ask ===")
    print(f"{'档位':>10} {'n':>4} {'反向胜率(TWAP)':>15} {'反向赔率':>10} {'EV(反向)':>10}")
    for b in sorted(g):
        v = g[b]
        n = len(v)
        rev_wr = 1 - sum(1 for r in v if r["win_tw"]) / n
        rev_odds = 1 - statistics.mean(r["ask"] for r in v)
        print(f"{b:>10} {n:>4} {rev_wr:>14.1%} {rev_odds:>10.3f} {rev_wr-rev_odds:>+10.3f}")

    print(f"\n=== ③ 两套口径的判定分歧 ===")
    disc = sum(1 for r in rows if r["out_tw"] != r["out_sp"])
    print(f"  即时口径与 TWAP 口径结算结果不一致: {disc}/{len(rows)} ({disc/len(rows):.1%})")
    print(f"  其中 我们判赢 / PM 判输: "
          f"{sum(1 for r in rows if r['win_sp'] and not r['win_tw'])}")
    print(f"  其中 我们判输 / PM 判赢: "
          f"{sum(1 for r in rows if r['win_tw'] and not r['win_sp'])}")

    print(f"\n=== ④ 方向 × 档位（PM 口径） ===")
    for d in ("up", "down"):
        print(f"  [{d}]")
        for b in sorted(g):
            sub = [r for r in g[b] if r["dir"] == d]
            if not sub:
                continue
            n = len(sub)
            wr = sum(1 for r in sub if r["win_tw"]) / n
            ma = statistics.mean(r["ask"] for r in sub)
            print(f"    {b}: n={n:>3} 胜率={wr:6.1%} ask={ma:.3f} EV={wr-ma:+.3f}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 24)
