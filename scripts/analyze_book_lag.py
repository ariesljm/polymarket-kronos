"""阶段0 盘口滞后标定：BTC 突破后 Polymarket token 盘口是否滞后跟随。

数据：data/recordings/calib_book_1s.csv（record_join_ws.py 采集的
ts, binance_price, up_ask, up_bid, down_ask, down_bid）。

核心问题：BTC 朝某方向突破后 N 秒，token 盘口是否已跟上？
- 若盘口滞后且滞后幅度 > bid-ask spread + 滑点 → 存在套利空间（盘口滞后套利）
- 若盘口同步或滞后幅度 < spread → 无套利空间

算法：
1. 按 15m 窗口对齐 ts，每窗口 base = 首个 binance_price
2. binance 偏移 = (price - base) / base * 100
3. 找 BTC 突破事件：binance 偏移绝对值超 thr（默认 0.05%）且达窗口内新极值的时刻 t0
4. 事件后 30/60s，up/down ask 是否同向移动、移动幅度
5. 套利空间 = 盘口滞后幅度 - 当时刻 spread

数据不足时输出诊断（覆盖时长、窗口数、事件数），不下结论。

用法：uv run python scripts/analyze_book_lag.py [--csv data/recordings/calib_book_1s.csv]
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

WINDOW_MS = 900_000  # 15m
THR_PCT = 0.05  # BTC 突破阈值


def load(csv_path: Path) -> list[dict]:
    rows = []
    with open(csv_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts = int(r["ts"])
                bp = float(r["binance_price"]) if r.get("binance_price") else None
                ua = float(r["up_ask"]) if r.get("up_ask") else None
                ub = float(r["up_bid"]) if r.get("up_bid") else None
                da = float(r["down_ask"]) if r.get("down_ask") else None
                db = float(r["down_bid"]) if r.get("down_bid") else None
            except (KeyError, ValueError):
                continue
            rows.append({"ts": ts, "bp": bp, "ua": ua, "ub": ub, "da": da, "db": db})
    return rows


def analyze(rows: list[dict]) -> None:
    n = len(rows)
    if n < 60:
        print(f"数据不足：仅 {n} 行（<60，约 1 分钟），无法标定。")
        print(f"覆盖时长：{(rows[-1]['ts']-rows[0]['ts'])/1000:.0f}s")
        print("需积累数小时（含 BTC 波动期）后重跑。")
        return

    # 按窗口分组
    wins = defaultdict(list)
    for r in rows:
        w = r["ts"] - (r["ts"] % WINDOW_MS)
        wins[w].append(r)
    for w in wins:
        wins[w].sort(key=lambda x: x["ts"])

    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600000
    complete = [w for w, pts in wins.items() if len(pts) >= 100]  # 至少 ~100s 采样
    print(f"=== 数据概况 ===")
    print(f"采样点 {n} 行，覆盖 {span_h:.2f}h，{len(wins)} 窗口（完整窗口 {len(complete)}）")

    # 统计盘口覆盖（盘口非空的行）
    with_book = sum(1 for r in rows if r["ua"] is not None)
    print(f"盘口非空行：{with_book}/{n} = {with_book/n*100:.0f}%（bot 未跑/窗口空档时留空）")

    # 找 BTC 突破事件
    events = []  # (ts, dir(+1/-1), offset%, ua_t0, da_t0, spread_t0)
    for w, pts in wins.items():
        if len(pts) < 30:
            continue
        base = next((p["bp"] for p in pts if p["bp"] is not None), None)
        if base is None:
            continue
        max_seen = base
        min_seen = base
        for p in pts:
            if p["bp"] is None:
                continue
            off = (p["bp"] - base) / base * 100
            # 上破：创窗口新高且偏移>thr
            if p["bp"] > max_seen and off > THR_PCT and p["ua"] is not None:
                spread = (p["ua"] - p["ub"]) if (p["ua"] and p["ub"]) else None
                events.append({"ts": p["ts"], "dir": 1, "off": off, "ua": p["ua"], "da": p["da"], "spread": spread})
            if p["bp"] < min_seen and off < -THR_PCT and p["da"] is not None:
                spread = (p["da"] - p["db"]) if (p["da"] and p["db"]) else None
                events.append({"ts": p["ts"], "dir": -1, "off": off, "ua": p["ua"], "da": p["da"], "spread": spread})
            max_seen = max(max_seen, p["bp"])
            min_seen = min(min_seen, p["bp"])

    print(f"\n=== BTC 突破事件（偏移>±{THR_PCT}%）===")
    print(f"事件数：{len(events)}（上破 {sum(1 for e in events if e['dir']>0)} / 下破 {sum(1 for e in events if e['dir']<0)}）")
    if len(events) < 10:
        print("事件数 <10，样本不足，不下结论。需更长波动期数据。")
        return

    # 窗口内每个事件后 30/60s 盘口跟随
    by_ts = {r["ts"]: r for r in rows}
    all_ts = sorted(by_ts.keys())
    follow_dir = defaultdict(lambda: [0, 0])  # offset -> [同向, 总]
    follow_mag = defaultdict(list)  # offset -> 盘口移动幅度%
    spreads = [e["spread"] for e in events if e["spread"] is not None]

    for e in events:
        for off_s in (30, 60):
            target = e["ts"] + off_s * 1000
            # 找最接近的采样
            near_ts = min(all_ts, key=lambda t: abs(t - target))
            if abs(near_ts - target) > 5000:
                continue
            nr = by_ts[near_ts]
            if nr["ua"] is None:
                continue
            if e["dir"] > 0:  # BTC 上破，up_ask 应涨
                book_change = (nr["ua"] - e["ua"])  # token 价 0-1 绝对变化
            else:  # BTC 下破，down_ask 应涨（down 概率升）
                if e["da"] is None or nr["da"] is None:
                    continue
                book_change = (nr["da"] - e["da"])
            follow_dir[off_s][1] += 1
            if book_change * e["dir"] > 0:  # 同向
                follow_dir[off_s][0] += 1
            follow_mag[off_s].append(book_change * 1000)  # 基点（×1000 因为 token 价 0-1）

    print(f"\n=== 盘口跟随 BTC 突破的方向一致性（滞后诊断）===")
    for off in (30, 60):
        a, t = follow_dir[off]
        if t:
            mags = follow_mag[off]
            print(f"  突破后 {off}s 盘口同向: {a}/{t}={a/t*100:.1f}%  盘口移动中位 {statistics.median(mags):+.1f}基点 P75={sorted(mags)[int(len(mags)*0.75)]:+.1f}基点")
    if spreads:
        print(f"  事件时刻 spread 中位: {statistics.median(spreads):.3f}（套利空间需 > spread+滑点）")

    # 套利空间估算
    arbable = 0
    for e in events:
        target = e["ts"] + 30 * 1000
        near_ts = min(all_ts, key=lambda t: abs(t - target))
        if abs(near_ts - target) > 5000:
            continue
        nr = by_ts[near_ts]
        if e["dir"] > 0 and e["ua"] is not None and nr["ua"] is not None:
            move = (nr["ua"] - e["ua"])
        elif e["dir"] < 0 and e["da"] is not None and nr["da"] is not None:
            move = (nr["da"] - e["da"])
        else:
            continue
        spread = e["spread"] or 0.01
        if move * 1000 > (spread + 0.005) * 1000:  # 盘口移动 > spread + 0.5基点滑点
            arbable += 1
    print(f"\n=== 套利空间（盘口 30s 移动 > spread+滑点）===")
    print(f"  可套利事件: {arbable}/{len(events)} = {arbable/len(events)*100:.0f}%")

    print(f"\n=== 定论 ===")
    best_a, best_t = max(follow_dir.values(), key=lambda x: x[0]/x[1] if x[1] else 0) if any(follow_dir.values()) else (0, 1)
    if best_t and best_a / best_t > 0.60 and arbable / len(events) > 0.30:
        print("  ✓ BTC 突破后盘口显著同向跟随且滞后 → 盘口滞后套利有实证基础，可推进择时入场")
    elif best_t and best_a / best_t > 0.60:
        print("  ~ 盘口同向跟随但滞后幅度不足覆盖 spread+滑点 → 套利空间存疑，需更长数据")
    else:
        print("  ✗ 盘口未显著同向跟随 BTC 突破 → 无盘口滞后套利基础")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default="data/recordings/calib_book_1s.csv")
    args = ap.parse_args()
    p = Path(args.csv)
    if not p.is_file():
        print(f"找不到 {p}"); return 1
    rows = load(p)
    analyze(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
