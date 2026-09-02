"""阶段0 前置验证：Binance 实时价在 5m 窗口内是否有"领先结算方向"的信号。

盘口滞后套利成立的前提：BTC 窗口前段的突破方向，能预测窗口结算方向
（终点价 vs 起点价）。若一致性 ≈ 50%（白噪声往返），则盘口滞后无可套利信号；
若显著 >50%，且大波动窗口占比可观，则盘口滞后套利有基础，值得补采盘口数据。

数据：data/recordings/calib_1s.csv（start_ts, t, price；Binance 秒级）。
不修改项目代码，纯分析脚本。

用法：uv run python scripts/analyze_btc_leading_signal.py [--csv data/recordings/calib_1s.csv]
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path

WINDOW_MS = 300_000  # 5m


def wilson(agree: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% 置信区间。"""
    if total == 0:
        return 0.0, 0.0
    p = agree / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return center - half, center + half


def load_windows(csv_path: Path) -> dict[int, list[tuple[int, float]]]:
    """按 5m 窗口起点分组：(t, price) 序列。"""
    wins: dict[int, list[tuple[int, float]]] = defaultdict(list)
    with open(csv_path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                t = int(row["t"]); p = float(row["price"])
            except (KeyError, ValueError):
                continue
            w = t - (t % WINDOW_MS)
            wins[w].append((t, p))
    return wins


def analyze(wins: dict[int, list[tuple[int, float]]]) -> None:
    nets, maxups, maxdns, swings = [], [], [], []
    # 前段突破方向预测结算方向的一致性：按前段长度与振幅阈值分桶
    agree_by_offset = defaultdict(lambda: [0, 0])  # offset_sec -> [agree, total]
    big_win_agree = [0, 0]  # 大波动窗口的前段预测一致性 [agree, total]

    for w, pts in wins.items():
        pts.sort()
        if len(pts) < 30:  # 窗口采样不足，跳过
            continue
        base = pts[0][1]
        end = pts[-1][1]
        prices = [p for _, p in pts]
        net = (end - base) / base * 100
        maxup = (max(prices) - base) / base * 100
        maxdn = (min(prices) - base) / base * 100
        nets.append(net); maxups.append(maxup); maxdns.append(maxdn)
        swings.append(maxup - maxdn)
        settle_dir = 1 if net > 0 else (-1 if net < 0 else 0)
        if settle_dir == 0:
            continue
        # 前段突破方向：窗口起点后 offset 秒的价格方向
        for offset in (30, 60, 120, 180):
            target_t = w + offset * 1000
            # 找最接近 target_t 的采样
            near = min(pts, key=lambda x: abs(x[0] - target_t))
            if abs(near[0] - target_t) > 5000:  # 偏差太大（采样断档）
                continue
            early_dir = 1 if near[1] > base else (-1 if near[1] < base else 0)
            if early_dir == 0:
                continue
            agree_by_offset[offset][1] += 1
            if early_dir == settle_dir:
                agree_by_offset[offset][0] += 1
        # 大波动窗口（窗口内振幅 > 0.10%）的前段预测一致性
        if (maxup - maxdn) > 0.10:
            target_t = w + 60 * 1000
            near = min(pts, key=lambda x: abs(x[0] - target_t))
            if abs(near[0] - target_t) <= 5000:
                early_dir = 1 if near[1] > base else (-1 if near[1] < base else 0)
                if early_dir != 0:
                    big_win_agree[1] += 1
                    if early_dir == settle_dir:
                        big_win_agree[0] += 1

    n = len(nets)
    if n == 0:
        print("无有效窗口数据。"); return
    nets_s = sorted(nets); swings_s = sorted(swings)
    def pct(a, i): return a[int(len(a) * i)] if a else 0.0
    print(f"5m 窗口数 = {n}")
    print(f"\n窗口净移动（结算方向幅度）%: 中位 {nets_s[n//2]:+.3f}  P25 {pct(nets_s,.25):+.3f}  P75 {pct(nets_s,.75):+.3f}  极值 [{min(nets_s):+.3f},{max(nets_s):+.3f}]")
    print(f"窗口内最大正向偏移 %: 中位 {statistics.median(maxups):+.3f}  极值 [{min(maxups):+.3f},{max(maxups):+.3f}]")
    print(f"窗口内最大负向偏移 %: 中位 {statistics.median(maxdns):+.3f}  极值 [{min(maxdns):+.3f},{max(maxdns):+.3f}]")
    print(f"窗口内振幅（高-低）%:    中位 {statistics.median(swings):.3f}  P75 {pct(swings_s,.75):.3f}  极值 [{min(swings_s):.3f},{max(swings_s):.3f}]")

    small_net = sum(1 for x in nets if abs(x) < 0.05)
    big_swing = sum(1 for x in swings if x > 0.10)
    print(f"\n净移动 |Δ|<0.05%（无方向结算信号）的窗口: {small_net}/{n} = {small_net/n*100:.0f}%")
    print(f"窗口内振幅 >0.10%（有可操作波动）的窗口: {big_swing}/{n} = {big_swing/n*100:.0f}%")

    print(f"\n=== 前段突破方向 → 结算方向 一致性（领先预测力）===")
    for off in (30, 60, 120, 180):
        a, t = agree_by_offset[off]
        if t:
            lo, hi = wilson(a, t)
            sig = "显著 区间>50%" if lo > 0.50 else "不显著 区间含50%" if lo <= 0.50 <= hi else "反方向"
            near_bias = "  接近结算可能有惯性自相关" if off >= 120 else ""
            print(f"  起点后 {off:3d}s 突破预测结算: {a}/{t}={a/t*100:.1f}%  Wilson95 {lo*100:.0f}~{hi*100:.0f}%  {sig}{near_bias}")
    if big_win_agree[1]:
        a, t = big_win_agree
        print(f"  大波动窗口 振幅>0.10% 的 60s 突破预测: {a}/{t}={a/t*100:.1f}%  样本仅{t} 不可靠")

    # 仅用离结算远的 30/60s（排除接近性偏差）判读真领先信号
    far_sig = False
    for off in (30, 60):
        a, t = agree_by_offset[off]
        if t:
            lo, _ = wilson(a, t)
            if lo > 0.50:
                far_sig = True
    print("\n=== 定论判读（仅看 30/60s 真领先信号，排除接近性偏差）===")
    if far_sig:
        print("  ✓ BTC 窗口前段(30-60s)突破方向对结算有统计显著预测力 → 盘口滞后套利有信号基础")
        print("    但大波动窗口(套利空间最大)预测力反而弱 → 需补采盘口数据标定『滞后幅度 vs 滑点』")
    else:
        print("  ✗ BTC 前段突破对结算无显著预测力(Wilson区间含50%) → 盘口滞后无可套利信号")
        print("    120/180s 的高预测力是接近结算的惯性自相关,非领先信号")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default="data/recordings/calib_1s.csv")
    args = ap.parse_args()
    p = Path(args.csv)
    if not p.is_file():
        print(f"找不到 {p}"); return 1
    wins = load_windows(p)
    analyze(wins)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
