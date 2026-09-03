"""用 Polymarket 结算口径（60s Chainlink TWAP 近似）重算预测记录 evaluate 结果。

背景：在线评估原用"目标窗口结束瞬时 close"判定方向，而 Polymarket 5m/15m/4h
市场自 8/7 起用 Chainlink 60s TWAP 结算、8/14 起 5m 也切 60s。本脚本把
predictions_<symbol>.csv 的 evaluated/correct 按新口径一次性重算
（最后 60 秒 1m K 线 HLOC 平均 vs baseline），供启动新版策略前对账。

用法: uv run python scripts/recalc_predictions_twap.py [--symbol BTC] [--data-dir data]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")
from pmbot.data_source import fetch_klines_batch  # noqa: E402

STEP_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def settle_price(target_ts: int, step: int) -> float | None:
    """目标窗口 [target_ts, target_ts+step) 最后 60 秒均价（1m HLOC 平均；失败 None）。"""
    import requests

    end = target_ts + step
    try:
        ks = fetch_klines_batch("BTC", "1m", end - 60_000, 10, proxies=None)
    except (requests.RequestException, ValueError, IndexError):
        return None
    last = [k for k in ks if end - 60_000 <= k.timestamp < end]
    if not last:
        return None
    k = last[-1]
    return (k.open + k.high + k.low + k.close) / 4.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--interval", default="5m", choices=STEP_MS)
    args = ap.parse_args()

    path = Path(args.data_dir) / f"predictions_{args.symbol.lower()}.csv"
    if not path.is_file():
        print(f"无预测记录文件: {path}")
        return 1

    df = pd.read_csv(path)
    step = STEP_MS[args.interval]

    old_correct = 0
    old_total = 0
    new_correct = 0
    new_total = 0
    fallback = 0
    flipped: list[str] = []
    for idx, row in df.iterrows():
        evaluated = bool(row["evaluated"]) and row["correct"] is not None and not pd.isna(row["correct"])
        if not evaluated:
            continue
        target_ts = int(row["ts"])
        pred_up = row["direction"] == "up"
        ref = settle_price(target_ts, step)
        if ref is None:
            fallback += 1  # 1m 不可得：不改写（保留旧值），不计入新口径统计
            continue
        old_ok = int(row["correct"])
        old_correct += old_ok
        old_total += 1
        ok = int((ref > float(row["baseline_close"])) == pred_up)
        new_correct += ok
        new_total += 1
        df.at[idx, "correct"] = ok
        if ok != old_ok:
            flipped.append(
                f"  {pd.Timestamp(target_ts, unit='ms', tz='UTC')}  "
                f"{'预测up' if pred_up else '预测down'}  "
                f"baseline={row['baseline_close']:.2f}  结算均价≈{ref:.2f}  "
                f"旧close口径 {'✓' if old_ok else '✗'} → 新TWAP口径 {'✓' if ok else '✗'}"
            )

    df.to_csv(path, index=False)
    print(f"记录总数: {len(df)}（已评估且可重算 {new_total}）")
    if old_total:
        print(f"旧口径（窗口结束瞬时 close）: {old_correct}/{old_total} = {old_correct/old_total:.1%}")
    if new_total:
        print(f"新口径（最后 60s TWAP 近似） : {new_correct}/{new_total} = {new_correct/new_total:.1%}")
    print(f"1m 数据不可得（保留旧值）: {fallback}")
    if flipped:
        print("方向翻转窗口:")
        print("\n".join(flipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())