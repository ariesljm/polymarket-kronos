"""Kronos 预测 × Binance 实时价 联合信号分析。

核心问题：Kronos（历史 K线 pattern）与 Binance 实时价（窗口内已实现走势）
是两个独立信号源。结合能否提升结算方向预测？

数据对齐：
- predictions_btc.csv: ts(目标窗口起点 ms), p_up, evaluated, correct
- calib_1s.csv: t(采样时刻 ms), price

每窗口：
- kronos_dir = 1 if p_up>0.5 else -1
- bin_60s_dir = sign(price@ts+60s - price@ts)  （窗口内前 60s 的 BTC 突破）
- settle_dir = kronos_dir if correct else -kronos_dir  （反推结算方向）

统计：单独准确率、同向准确率（双重确认）、冲突准确率、联合策略（同向才入场）。

用法：uv run python scripts/analyze_kronos_binance_combine.py
"""
from __future__ import annotations

import csv
from collections import defaultdict


def load_preds() -> list:
    rows = []
    with open("data/predictions_btc.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if str(r.get("evaluated", "")) != "1":
                continue
            try:
                ts = int(r["ts"]); p_up = float(r["p_up"])
            except (KeyError, ValueError):
                continue
            correct = str(r.get("correct", "")).startswith("1")
            rows.append({"ts": ts, "p_up": p_up, "correct": correct})
    return rows


def load_bin_1m(ts_lo: int, ts_hi: int) -> dict:
    """拉 predictions 时段的 1m K线，按 5m 窗口分组，取每窗口第 1 根 1m（前 60s）。

    bin_60s_dir = sign(1m.close - 1m.open)：1m open≈窗口起点价，close≈ts+60s 价。
    """
    from pmbot.data_source import fetch_klines_batch
    since = ts_lo
    all_k = []
    while since < ts_hi:
        try:
            ks = fetch_klines_batch("BTC", "1m", since, 1000)
        except Exception as e:
            print(f"拉 1m 失败 @ {since}: {e}"); break
        if not ks: break
        all_k.extend(ks)
        since = ks[-1].timestamp + 60_000
        if len(ks) < 1000: break
    print(f"拉 1m K线 {len(all_k)} 根")
    wins = defaultdict(list)
    for k in all_k:
        w = k.timestamp - (k.timestamp % 300_000)
        if k.timestamp < w + 60_000:  # 仅取窗口第 1 根 1m（前 60s）
            wins[w].append(k)
    return wins


def main() -> int:
    preds = load_preds()
    if not preds:
        print("无 evaluated predictions"); return 1
    ts_lo = min(p["ts"] for p in preds)
    ts_hi = max(p["ts"] for p in preds) + 300_000
    wins = load_bin_1m(ts_lo, ts_hi)
    print(f"predictions evaluated={len(preds)}, 覆盖时段 1m 窗口={len(wins)}")

    rows = []
    for pr in preds:
        w = pr["ts"]
        ks = wins.get(w)
        if not ks:
            continue
        k = ks[0]  # 窗口第 1 根 1m（前 60s）
        kronos_dir = 1 if pr["p_up"] > 0.5 else -1
        bin_dir = 1 if k.close > k.open else (-1 if k.close < k.open else 0)
        if bin_dir == 0:
            continue
        settle_dir = kronos_dir if pr["correct"] else -kronos_dir
        rows.append({"kronos": kronos_dir, "bin": bin_dir, "settle": settle_dir,
                     "p_up": pr["p_up"]})

    n = len(rows)
    if n < 10:
        print(f"重叠窗口 {n} <10，无法分析（predictions 与 calib_1s 时间不重叠）")
        return 1

    def acc(field_filter=None) -> tuple[int, int]:
        a = t = 0
        for r in rows:
            if field_filter and not field_filter(r):
                continue
            t += 1
            if r["kronos" if False else "bin"] == r["settle"]:  # placeholder
                pass
        return a, t

    # 各信号单独准确率
    k_ok = sum(1 for r in rows if r["kronos"] == r["settle"])
    b_ok = sum(1 for r in rows if r["bin"] == r["settle"])
    print(f"\n=== 重叠窗口 {n} 个 ===")
    print(f"Kronos 单独准确率: {k_ok}/{n} = {k_ok/n*100:.1f}%")
    print(f"Binance60s 单独准确率: {b_ok}/{n} = {b_ok/n*100:.1f}%")

    # 同向（双重确认）
    same = [r for r in rows if r["kronos"] == r["bin"]]
    same_ok = sum(1 for r in same if r["bin"] == r["settle"])
    print(f"\n=== 同向（Kronos==Binance, 双重确认）===")
    if same:
        print(f"  准确率: {same_ok}/{len(same)} = {same_ok/len(same)*100:.1f}%  覆盖 {len(same)/n*100:.0f}%")

    # 冲突
    conf = [r for r in rows if r["kronos"] != r["bin"]]
    conf_bin_ok = sum(1 for r in conf if r["bin"] == r["settle"])
    conf_kronos_ok = sum(1 for r in conf if r["kronos"] == r["settle"])
    print(f"\n=== 冲突（Kronos != Binance）===")
    if conf:
        print(f"  冲突占比: {len(conf)/n*100:.0f}%")
        print(f"  冲突时 Binance 胜: {conf_bin_ok}/{len(conf)} = {conf_bin_ok/len(conf)*100:.0f}%")
        print(f"  冲突时 Kronos 胜: {conf_kronos_ok}/{len(conf)} = {conf_kronos_ok/len(conf)*100:.0f}%")

    # 联合策略：同向才入场（冲突跳过）
    print(f"\n=== 联合策略：同向才入场（冲突跳过）===")
    print(f"  入场 {len(same)}/{n} = {len(same)/n*100:.0f}% 覆盖")
    if same:
        print(f"  入场准确率 {same_ok/len(same)*100:.1f}%  → 整体期望（准确率×覆盖）{same_ok/n*100:.1f}%")
        print(f"  vs Kronos 单独 {k_ok/n*100:.1f}%  vs Binance 单独 {b_ok/n*100:.1f}%")

    # 高置信 Kronos + Binance 同向
    hi_same = [r for r in same if r["p_up"] >= 0.9 or r["p_up"] <= 0.1]
    hi_ok = sum(1 for r in hi_same if r["bin"] == r["settle"])
    print(f"\n=== 强信号 Kronos P≥0.9/≤0.1 且与 Binance 同向 ===")
    if hi_same:
        print(f"  准确率: {hi_ok}/{len(hi_same)} = {hi_ok/len(hi_same)*100:.1f}%  覆盖 {len(hi_same)/n*100:.0f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
