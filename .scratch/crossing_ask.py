"""首次穿越阈值那一刻的真实 ask 分布（回答"穿越后价格还接近 0.5 吗"）。

背景：拒单日志是一个被 cap 截断的样本——ask ≤ 0.50 的机会会成交而不是被拒，
所以"拒单最低 ask = 0.63"无法回答"穿越瞬间价格是否接近 0.5"。
本脚本绕开 cap，直接看秒级盘口：穿越发生的那一秒，我们想买的那个 token 的 ask 是多少。

方法（数据 data/recordings/calib_book_1s.csv，1s 分辨率：binance_price + up/down ask/bid）：
  1. 按 5m 对齐分窗（ts 秒数 // 300 * 300），窗口起点价 = 窗口内首个有效 binance_price
  2. 逐秒算偏离 (price-open)/open，找**首次**越过 ±阈值 的那一秒
  3. 记录该秒"要买方向"的 ask，以及穿越后 +1/+2/+3/+5/+10s 的 ask（测极化速度）
  4. 用窗口内 close vs open 判胜负 → 命中率 q、EV = q − 假定入场价

关键输出：
  - 各阈值下"首次穿越 ask"的分布与 ≤0.50 占比（回答本问题）
  - ask 从穿越后 1s 到 10s 的漂移（测"入场窗口有多宽"）
  - 阈值 ↓ 是否真能拿到更低 ask（回答"下调阈值有没有用"）

用法: uv run python .scratch/crossing_ask.py [--thresholds 0.05,0.08,...]
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CSV = Path("data/recordings/calib_book_1s.csv")
STEP = 300  # 5m 窗口（秒）
MIN_WINDOW_SEC = 200  # 少于这么多秒的窗口丢弃（首尾不完整窗）


def load() -> list[tuple[int, float, float | None, float | None]]:
    """→ [(ts_sec, price, up_ask, down_ask)]，只保留有价格的秒。"""
    rows = []
    with open(CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            p, ua, da = r.get("binance_price"), r.get("up_ask"), r.get("down_ask")
            if not p:
                continue
            try:
                ts = int(r["ts"]) // 1000
                price = float(p)
                up = float(ua) if ua else None
                dn = float(da) if da else None
            except (ValueError, TypeError):
                continue
            rows.append((ts, price, up, dn))
    rows.sort()
    return rows


def build_windows(rows) -> dict[int, dict[int, tuple[float, float | None, float | None]]]:
    """→ {win_start: {ts: (price, up_ask, down_ask)}}，丢弃过短窗口。"""
    win: dict[int, dict] = defaultdict(dict)
    for ts, price, up, dn in rows:
        win[ts - (ts % STEP)][ts] = (price, up, dn)
    return {w: s for w, s in win.items() if len(s) >= MIN_WINDOW_SEC}


def analyze(win: dict, thresholds: list[float]) -> dict[float, list[dict]]:
    """每个阈值 → 首次穿越记录列表。"""
    out: dict[float, list[dict]] = {t: [] for t in thresholds}
    for w, secs in win.items():
        ts_list = sorted(secs)
        open_p = secs[ts_list[0]][0]
        if not open_p:
            continue
        close_p = secs[ts_list[-1]][0]
        for thr in thresholds:
            hit_dir = None
            for ts in ts_list:
                price, up_ask, dn_ask = secs[ts]
                dev = (price - open_p) / open_p * 100
                if dev >= thr and up_ask is not None:
                    hit_dir, ask, d = "up", up_ask, ts
                    break
                if dev <= -thr and dn_ask is not None:
                    hit_dir, ask, d = "down", dn_ask, ts
                    break
            if hit_dir is None:
                continue
            # 穿越后 N 秒的 ask（该方向）
            later = {}
            for lag in (1, 2, 3, 5, 10):
                v = secs.get(d + lag)
                if v is not None:
                    a = v[1] if hit_dir == "up" else v[2]
                    if a is not None:
                        later[lag] = a
            won = (close_p > open_p) if hit_dir == "up" else (close_p < open_p)
            out[thr].append({
                "win": w, "dir": hit_dir, "ask": ask, "into": d - w,
                "later": later, "won": won,
            })
    return out


def pct(n: int, d: int) -> str:
    return f"{n/d*100:5.1f}%" if d else "  --  "


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", default="0.05,0.08,0.10,0.12,0.15,0.20")
    args = ap.parse_args()
    thresholds = [float(x) for x in args.thresholds.split(",")]

    if not CSV.is_file():
        print(f"找不到 {CSV}")
        return 1
    rows = load()
    win = build_windows(rows)
    print(f"秒级样本 {len(rows)} 行（有价） → 完整 5m 窗口 {len(win)} 个")
    span = max(win) - min(win)
    print(f"覆盖 {span/86400:.1f} 天\n")

    res = analyze(win, thresholds)

    print("=== 各阈值：首次穿越时的 ask 分布（回答'穿越后价格还接近 0.5 吗'）===")
    print(f"{'阈值':>6} | {'触发':>6} | {'ask中位':>7} | {'ask≤0.50':>9} | {'ask≤0.55':>9} | {'ask≤0.60':>9} | {'穿越秒数中位':>11}")
    for thr in thresholds:
        v = res[thr]
        if not v:
            print(f"{thr:6.2f} | {0:6d} |     -- |       -- |       -- |       -- |          --")
            continue
        asks = [x["ask"] for x in v]
        print(f"{thr:6.2f} | {len(v):6d} | {statistics.median(asks):7.3f} | "
              f"{pct(sum(a <= 0.50 for a in asks), len(asks)):>9} | "
              f"{pct(sum(a <= 0.55 for a in asks), len(asks)):>9} | "
              f"{pct(sum(a <= 0.60 for a in asks), len(asks)):>9} | "
              f"{statistics.median(x['into'] for x in v):11.0f}")

    print("\n=== 穿越后 ask 漂移（测入场窗口有多宽；取各阈值汇总）===")
    print(f"{'阈值':>6} | {'穿越时':>7} | {'+1s':>7} | {'+2s':>7} | {'+3s':>7} | {'+5s':>7} | {'+10s':>7}")
    for thr in thresholds:
        v = res[thr]
        if not v:
            continue
        def med(key):
            vals = [x["ask"] if key == 0 else x["later"].get(key) for x in v]
            vals = [a for a in vals if a is not None]
            return statistics.median(vals) if vals else float("nan")
        print(f"{thr:6.2f} | {med(0):7.3f} | {med(1):7.3f} | {med(2):7.3f} | "
              f"{med(3):7.3f} | {med(5):7.3f} | {med(10):7.3f}")

    print("\n=== 若在'首次穿越价'入场：命中率与 EV ===")
    print(f"{'阈值':>6} | {'触发':>5} | {'均价 p':>7} | {'命中率 q':>8} | {'EV/笔':>7} | {'EV(≤0.50 子集)':>14}")
    for thr in thresholds:
        v = res[thr]
        if not v:
            continue
        p = statistics.mean(x["ask"] for x in v)
        q = sum(x["won"] for x in v) / len(v)
        sub = [x for x in v if x["ask"] <= 0.50]
        if sub:
            ps = statistics.mean(x["ask"] for x in sub)
            qs = sum(x["won"] for x in sub) / len(sub)
            sub_s = f"n={len(sub)} EV={qs-ps:+.3f}"
        else:
            sub_s = "n=0"
        print(f"{thr:6.2f} | {len(v):5d} | {p:7.3f} | {q*100:7.1f}% | {q-p:+7.3f} | {sub_s:>14}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
