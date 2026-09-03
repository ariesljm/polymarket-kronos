"""独立复核 Kronos 基线实证：不复用 baseline.py 的计算逻辑。

验证三件事：
1. 结算标签真实性：随机抽样窗口重新请求 gamma API 比对 up_wins
2. 窗口对齐：Binance 判向 vs 结算结果一致率（若对齐错位会掉到 ~50%）
   同时检查「错位一格」假设下的一致率，确认当前对齐是最优解
3. 准确率/校准独立重算 + Wilson 区间 + 分桶

用法: uv run python scripts/reverify_baseline.py [--spot N]
"""
from __future__ import annotations

import json
import math
import random
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

sys.path.insert(0, ".")
from pmbot.backtest import fetch_klines  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
STEP = 300


def wilson_lb(correct: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = correct / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (center - margin) / denom


def fetch_label(ts: int) -> tuple[int, int | None]:
    """返回 (window_start, up_wins|None)。None=未取到或未结算。"""
    try:
        r = requests.get(f"{GAMMA}/events", params={"slug": f"btc-updown-5m-{ts}"}, headers=UA, timeout=20)
        events = r.json()
        m = events[0]["markets"][0]
        outs = json.loads(m["outcomes"])
        ops = json.loads(m["outcomePrices"])
        return ts, int(ops[outs.index("Up")] == "1")
    except Exception:
        return ts, None


def main() -> None:
    o = pd.read_csv("data/baseline/outcomes_BTC_5m.csv")
    p = pd.read_csv("data/baseline/preds_BTC_5m.csv")
    j = o.merge(p, on="window_start").sort_values("window_start")
    ts_list = j.window_start.astype(int).tolist()
    up_wins = dict(zip(j.window_start.astype(int), j.up_wins.astype(int)))
    p_up = dict(zip(j.window_start.astype(int), j.p_up.astype(float)))
    n = len(ts_list)

    # ---- 2. Binance 判向交叉验证 ----
    df = fetch_klines("BTC", "5m", n + len(set(range(min(ts_list), max(ts_list) + STEP, STEP)) - set(ts_list)) + 10)
    closes = {int(t): float(c) for t, c in zip(df["timestamp"].astype("int64"), df["close"])}

    def binance_dir(ts: int, shift: int = 0) -> bool | None:
        """窗口 [ts, ts+300) 的方向 = close(ts+shift) > close(ts+shift-300)。shift=0 即原口径。"""
        c_now, c_prev = closes.get((ts + shift) * 1000), closes.get((ts + shift - STEP) * 1000)
        return None if c_now is None or c_prev is None else c_now > c_prev

    print("=" * 72)
    print("A. 窗口对齐检验（Binance 判向 vs Polymarket 结算）")
    print("=" * 72)
    for shift, name in [(0, "当前口径 [ts, ts+300)"), (-STEP, "错位假设 A：标签提前一格"),
                        (+STEP, "错位假设 B：标签推后一格")]:
        pairs = [(binance_dir(t, shift), bool(up_wins[t])) for t in ts_list]
        pairs = [(a, b) for a, b in pairs if a is not None]
        agree = sum(a == b for a, b in pairs) / len(pairs)
        print(f"{name:>28}: 一致率 {agree:.1%} ({len(pairs)} 样本)")

    # ---- 3. 准确率 / 校准 ----
    print()
    print("=" * 72)
    print("B. 方向准确率独立重算（真相=结算结果）")
    print("=" * 72)
    correct = sum((p_up[t] > 0.5) == bool(up_wins[t]) for t in ts_list)
    acc = correct / n
    lb = wilson_lb(correct, n)
    hi = 1 - wilson_lb(n - correct, n)
    print(f"总体: {acc:.1%} ({correct}/{n})  Wilson95 CI [{lb:.1%}, {hi:.1%}]")

    print()
    tgt = {t: (p_up[t] if p_up[t] > 0.5 else 1 - p_up[t], p_up[t] > 0.5) for t in ts_list}
    print(f"{'置信桶':>12} {'n':>6} {'准确率':>8} {'平均p':>7} {'Wilson下限':>9}")
    for lo, hi_b in [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]:
        sub = [t for t in ts_list if lo <= tgt[t][0] < hi_b]
        if not sub:
            continue
        c = sum(tgt[t][1] == bool(up_wins[t]) for t in sub)
        avg_p = sum(tgt[t][0] for t in sub) / len(sub)
        print(f"{lo:.2f}-{hi_b:<5.2f} {len(sub):>6} {c/len(sub):>7.1%} {avg_p:>7.3f} {wilson_lb(c,len(sub)):>8.1%}")

    # 极端桶合并看（样本少时单桶噪声大）
    sub = [t for t in ts_list if tgt[t][0] >= 0.75]
    if sub:
        c = sum(tgt[t][1] == bool(up_wins[t]) for t in sub)
        print(f"\n合并高置信 p_target>=0.75: {c}/{len(sub)} = {c/len(sub):.1%}  Wilson下限 {wilson_lb(c,len(sub)):.1%}")

    # ---- 1. 结算标签抽查 ----
    spot = 24
    random.seed(42)
    sample = random.sample(ts_list, min(spot, len(ts_list)))
    print()
    print("=" * 72)
    print(f"C. 结算标签抽查（gamma API 重取 {len(sample)} 个随机窗口）")
    print("=" * 72)
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(fetch_label, sample))
    ok = mismatch = missing = 0
    for ts, lab in res:
        if lab is None:
            missing += 1
            continue
        if lab == up_wins[ts]:
            ok += 1
        else:
            mismatch += 1
            print(f"  不一致! window={ts} 缓存={up_wins[ts]} API={lab}")
    print(f"一致 {ok} / 不一致 {mismatch} / 取不到 {missing}")


if __name__ == "__main__":
    main()
