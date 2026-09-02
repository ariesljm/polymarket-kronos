"""跨频率 Kronos 方向预测准确率对比：5m vs 15m vs 1h。

回答：换更大波动的窗口（15m/1h）能否提升 Kronos 方向预测力？
机制上 pred_len 仍=1（预测下个窗口末价方向，与窗口长度无关），
本脚本只验证"不同频率下 Kronos 的方向准确率"。

算法：拉对应频率 K线 → 滑窗用前 ctx 根预测下 1 根 →
p_up = sample_count 条采样中 > baseline 的比例 → 预测方向 →
与实际方向（下根 close vs baseline）对比。

不修改项目代码，纯离线脚本（用 kronos-mini 避免与 bot 的 base 争 GPU 显存；
同一 variant 跨频率对比，趋势有效）。

用法：uv run python scripts/analyze_kronos_interval.py --tf 15m [--n-pred 100]
"""
from __future__ import annotations

import argparse
import warnings

import pandas as pd

warnings.filterwarnings("ignore")

from pmbot.data_source import BinanceDataSource, KlineStore
from pmbot.predictor import KronosPredictorClient
from pmbot.variant_map import VARIANT_CONTEXT


def run(tf: str, n_pred: int, variant: str, sample_count: int, ctx: int) -> list:
    import time
    from pmbot.data_source import fetch_klines_batch
    step = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}[tf]
    need = ctx + n_pred + 10
    since = int(time.time() * 1000) - need * step
    ks = fetch_klines_batch("BTC", tf, since, need)
    df = pd.DataFrame([{"timestamp": k.timestamp, "open": k.open, "high": k.high,
                        "low": k.low, "close": k.close, "volume": k.volume} for k in ks])
    df = df.sort_values("timestamp").reset_index(drop=True)
    print(f"[{tf}] 拉取 {len(df)} 根 K线（{variant}, ctx={ctx}, samples={sample_count}）")
    if len(df) < ctx + n_pred:
        print(f"数据不足：需 {ctx + n_pred} 根，仅 {len(df)} 根")
        return []

    client = KronosPredictorClient(variant=variant)
    results = []  # (p_up, correct, p_up_bin)
    import time
    t0 = time.time()
    for k in range(len(df) - n_pred, len(df)):
        if k < ctx:
            continue
        x = df.iloc[k - ctx:k].reset_index(drop=True)
        baseline = float(df["close"].iloc[k - 1])
        actual = float(df["close"].iloc[k])
        preds = client.predict_closes(x, sample_count)
        p_up = sum(1 for v in preds if v > baseline) / len(preds)
        pred_dir = 1 if p_up > 0.5 else -1
        actual_dir = 1 if actual > baseline else -1
        results.append((p_up, pred_dir == actual_dir))
        if (len(results)) % 10 == 0:
            el = time.time() - t0
            print(f"  [{tf}] {len(results)}/{n_pred}  累计准确率 {sum(r[1] for r in results)/len(results)*100:.0f}%  用时 {el:.0f}s")
    return results


def report(tf: str, results: list) -> None:
    if not results:
        print(f"[{tf}] 无结果"); return
    n = len(results)
    acc = sum(r[1] for r in results) / n
    # 按 P(up) 分桶
    bins = {"<0.9": [0, 0], ">=0.9": [0, 0]}
    for p, c in results:
        key = ">=0.9" if p >= 0.9 else "<0.9"
        bins[key][1] += 1
        if c: bins[key][0] += 1
    print(f"\n=== [{tf}] Kronos 方向准确率（{n} 个滑窗）===")
    print(f"  总体: {sum(r[1] for r in results)}/{n} = {acc*100:.1f}%")
    for k, (a, t) in bins.items():
        if t: print(f"  P(up){k}: {a}/{t} = {a/t*100:.0f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", default="15m", choices=["5m", "15m", "1h"], help="K线频率")
    ap.add_argument("--n-pred", type=int, default=100, help="滑窗预测数")
    ap.add_argument("--variant", default="kronos-mini", help="模型变体")
    ap.add_argument("--sample-count", type=int, default=50, help="采样路径数")
    ap.add_argument("--all", action="store_true", help="跑 5m/15m/1h 对比")
    args = ap.parse_args()

    ctx = min(512, VARIANT_CONTEXT.get(args.variant, 512))
    if args.all:
        for tf in ("5m", "15m", "1h"):
            r = run(tf, args.n_pred, args.variant, args.sample_count, ctx)
            report(tf, r)
    else:
        r = run(args.tf, args.n_pred, args.variant, args.sample_count, ctx)
        report(args.tf, r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
