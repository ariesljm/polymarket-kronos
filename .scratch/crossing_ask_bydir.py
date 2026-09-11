"""分方向看:穿越瞬间 ask 与穿越后漂移(up vs down 是否对称)。

crossing_ask.py 混合 up/down 输出 0.510；今天实盘 up 全 0.9+、down 0.24-0.50，
需分方向验证是"结构不对称"还是"延迟导致"。
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

CSV = Path("data/recordings/calib_book_1s.csv")
STEP = 300
MIN_WINDOW_SEC = 200
THR = 0.08


def load():
    rows = []
    with open(CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            p, ua, da = r.get("binance_price"), r.get("up_ask"), r.get("down_ask")
            if not p:
                continue
            try:
                ts = int(r["ts"]) // 1000
                rows.append((ts, float(p),
                             float(ua) if ua else None,
                             float(da) if da else None))
            except (ValueError, TypeError):
                continue
    rows.sort()
    return rows


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


def main() -> None:
    win: dict[int, dict] = defaultdict(dict)
    for ts, price, up, dn in load():
        win[ts - (ts % STEP)][ts] = (price, up, dn)
    win = {w: s for w, s in win.items() if len(s) >= MIN_WINDOW_SEC}

    # 方向 -> {offset -> [ask]}
    ask: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    # 方向 -> 命中(窗口 close vs open)
    hit: dict[str, list[int]] = defaultdict(list)
    OFFS = [0, 1, 2, 3, 5, 10]

    for w, secs in win.items():
        tss = sorted(secs)
        open_p = secs[tss[0]][0]
        close_p = secs[tss[-1]][0]
        if not open_p or close_p is None:
            continue
        hit_dir = None
        hit_idx = None
        for i, ts in enumerate(tss):
            price, up_ask, dn_ask = secs[ts]
            dev = (price - open_p) / open_p * 100
            if dev >= THR and up_ask is not None:
                hit_dir, hit_idx = "up", i
                break
            if dev <= -THR and dn_ask is not None:
                hit_dir, hit_idx = "down", i
                break
        if hit_dir is None:
            continue
        won = 1 if ((close_p - open_p) > 0) == (hit_dir == "up") else 0
        hit[hit_dir].append(won)
        for off in OFFS:
            j = hit_idx + off
            if j < len(tss):
                _, up_ask, dn_ask = secs[tss[j]]
                a = up_ask if hit_dir == "up" else dn_ask
                if a is not None:
                    ask[hit_dir][off].append(a)

    for d in ("up", "down"):
        n = len(hit[d])
        if not n:
            continue
        a0 = ask[d][0]
        print(f"\n=== {d.upper()}  触发 {n}  命中率 {sum(hit[d])/n:.1%} ===")
        print(f"  穿越瞬间 ask: 中位 {median(a0):.3f}  "
              f"≤0.50 {sum(1 for x in a0 if x<=0.50)/len(a0):.1%}  "
              f"≤0.60 {sum(1 for x in a0 if x<=0.60)/len(a0):.1%}")
        print("  漂移: " + "  ".join(
            f"+{o}s={median(ask[d][o]):.3f}" for o in OFFS if ask[d][o]))


if __name__ == "__main__":
    main()
