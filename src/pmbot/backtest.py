"""离线对比回测：15m 单步预测 vs 5m 三步预测的方向准确率。

评估口径（与在线/Polymarket 结算对齐）：
- 窗口 t（15m 对齐）：baseline = 窗口开始前一根闭合 K 线收盘
- 实际方向 = 窗口结束价（t+15m 时刻）> baseline
- 方案 A（15m）：输入 512 根 15m 闭合 K 线，单步预测 → 窗口结束价 vs baseline
- 方案 B（5m）：输入 512 根 5m 闭合 K 线，三步预测（终点 t+15m 恰为窗口结束价）vs baseline

用法: uv run python -m pmbot.backtest [--points 50] [--samples 5]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import asdict

import pandas as pd

from pmbot.constants import STEP_MS
from pmbot.data_source import fetch_klines_batch
from pmbot.variant_map import VARIANT_CONTEXT

# 代理优先环境变量（与 run.py 一致），无则回退本机默认代理
PROXIES = {"https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10808")}


def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """拉取 K 线（limit>1000 时分页向前拉，startTime 定位）。

    单页拉取委托 data_source.fetch_klines_batch（在线/离线共用同一批量拉取实现）。
    """
    rows: list[dict] = []
    since: int | None = None
    while len(rows) < limit:
        batch = fetch_klines_batch(symbol, interval, since, min(1000, limit - len(rows)), proxies=PROXIES)
        if not batch:
            break
        rows = [asdict(k) for k in batch] + rows  # 最新在前，往前翻页
        since = batch[0].timestamp - 1  # 下一批更早数据
        if len(batch) < min(1000, limit - len(rows) + len(batch)):
            break  # 到底了
    df = pd.DataFrame(rows)
    df["timestamp"] = df["timestamp"].astype("int64")
    return df


def split_points(df15: pd.DataFrame, n_points: int, max_context: int) -> list[int]:
    """取最后 n_points 个可评估窗口索引（输入 ≥ max_context 根闭合且窗口结束价已闭合）。"""
    return list(range(len(df15) - n_points, len(df15)))


def evaluate_predictions(preds: list[float], baseline: float, actual_close: float) -> tuple[bool, bool]:
    """(预测方向是否正确, 实际是否上涨)。预测方向 = 预测值 > baseline。"""
    pred_up = sum(1 for p in preds if p > baseline) / len(preds) > 0.5
    actual_up = actual_close > baseline
    return pred_up == actual_up, actual_up


def run_single_interval(args: argparse.Namespace) -> int:
    """单频率方向回测：预测下一根 vs 实际方向 + IC。"""
    from pmbot.predictor import KronosPredictorClient
    from scipy.stats import spearmanr

    ctx = VARIANT_CONTEXT[args.variant]
    need = ctx + args.points + 1
    print(f"拉取 {args.interval} 数据 × {need} 根（{args.symbol} / {args.variant}）...")
    df = fetch_klines(args.symbol, args.interval, need)
    print(f"  {len(df)} 根（{df['timestamp'].iloc[0]} ~ {df['timestamp'].iloc[-1]}）")

    client = KronosPredictorClient(variant=args.variant)

    ts = df["timestamp"].astype("int64").tolist()
    close = df["close"].tolist()
    correct = 0
    total = 0
    pred_rets, actual_rets, p_ups = [], [], []
    t0 = time.time()

    for i in range(len(df) - args.points, len(df)):
        baseline = close[i - 1]
        actual_close = close[i]
        actual_ret = actual_close / baseline - 1
        actual_rets.append(actual_ret)
        x = df.iloc[i - ctx : i].reset_index(drop=True)
        preds = client.predict_targets(x, args.samples)
        pred_ret = sum(preds) / len(preds) / baseline - 1
        p_up = sum(1 for v in preds if v > baseline) / len(preds)
        pred_rets.append(pred_ret)
        p_ups.append(p_up)
        correct += int((p_up > 0.5) == (actual_ret > 0))
        total += 1

    acc = correct / total if total else 0.0
    ic = spearmanr(pred_rets, actual_rets).statistic
    ic_p = spearmanr(p_ups, [1 if r > 0 else 0 for r in actual_rets]).statistic
    print()
    print(f"评估 {total} 个 {args.interval} 窗口（耗时 {time.time() - t0:.0f}s, {args.samples} 采样/窗口）")
    print(f"实际涨: {sum(1 for r in actual_rets if r > 0)}/{total} = {sum(1 for r in actual_rets if r > 0) / total:.1%}")
    print("=" * 56)
    print(f"方向准确率: {acc:.1%} ({correct}/{total})")
    print(f"IC(预测涨跌幅 vs 实际): {ic:.3f}")
    print(f"IC(P(up) vs 实际方向):  {ic_p:.3f}")
    print(f"预测涨跌幅均值 {sum(pred_rets) / len(pred_rets):.4%}  实际 {sum(actual_rets) / len(actual_rets):.4%}")
    print("=" * 56)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="15m vs 5m 预测准确率对比回测")
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--points", type=int, default=50, help="评估窗口数")
    parser.add_argument("--samples", type=int, default=5, help="每窗口采样次数")
    parser.add_argument("--variant", default="kronos-small", help="模型变体（kronos-mini/small/base）")
    parser.add_argument("--interval", default=None, help="单频率回测（15m/1h/4h/1d）；默认跑 15m vs 5m 对比")
    args = parser.parse_args(argv)

    if args.interval:
        return run_single_interval(args)

    from pmbot.predictor import KronosPredictorClient

    ctx = VARIANT_CONTEXT[args.variant]
    need15 = ctx + args.points + 1
    need5 = ctx + args.points * 3 + 1
    print(f"拉取数据：15m×{need15} 根，5m×{need5} 根（{args.symbol} / {args.variant}）...")
    df15 = fetch_klines(args.symbol, "15m", need15)
    df5 = fetch_klines(args.symbol, "5m", need5)
    print(f"  15m: {len(df15)} 根（{df15['timestamp'].iloc[0]} ~ {df15['timestamp'].iloc[-1]}）")
    print(f"  5m: {len(df5)} 根")

    client = KronosPredictorClient(variant=args.variant)

    # 5m → 每 15m 窗口内 3 根，需要按窗口对齐：5m 时间戳集合
    ts5_set = set(df5["timestamp"].astype("int64"))
    ts5 = df5["timestamp"].astype("int64")
    close5 = df5["close"].tolist()
    ts15 = df15["timestamp"].astype("int64")
    close15 = df15["close"].tolist()
    ts5_idx = {t: i for i, t in enumerate(ts5)}

    correct_a = correct_b = 0
    total = 0
    p_up_a_list, p_up_b_list = [], []
    disagreements = 0
    t0 = time.time()

    for i in split_points(df15, args.points, ctx):
        # ---- 窗口上下文 ----
        win_ts = ts15[i]            # 窗口开始（15m 对齐，也是 5m 对齐点）
        baseline = close15[i - 1]   # 窗口开始前一根 15m 收盘
        actual_close = close15[i]   # 窗口结束价（t+15m 时刻）
        actual_up = actual_close > baseline

        # ---- 方案 A：15m 单步 ----
        x_a = df15.iloc[i - ctx : i].reset_index(drop=True)
        preds_a = client.predict_targets(x_a, args.samples)
        ok_a, _ = evaluate_predictions(preds_a, baseline, actual_close)
        p_up_a = sum(1 for x in preds_a if x > baseline) / len(preds_a)
        p_up_a_list.append(p_up_a)

        # ---- 方案 B：5m 三步 ----
        j = ts5_idx.get(win_ts - 5 * 60 * 1000)  # 输入截止 t-5m 那根
        if j is None or j + 1 - ctx < 0:
            continue
        x_b = df5.iloc[j + 1 - ctx : j + 1].reset_index(drop=True)
        last5 = int(x_b["timestamp"].iloc[-1])
        preds_b = client.predict_targets(
            x_b,
            args.samples,
            pred_len=3,
            y_timestamps=[
                last5 + STEP_MS,
                last5 + STEP_MS * 2,
                last5 + STEP_MS * 3,
            ],
        )
        ok_b, _ = evaluate_predictions(preds_b, baseline, actual_close)
        p_up_b = sum(1 for x in preds_b if x > baseline) / len(preds_b)
        p_up_b_list.append(p_up_b)

        correct_a += ok_a
        correct_b += ok_b
        total += 1
        if (p_up_a > 0.5) != (p_up_b > 0.5):
            disagreements += 1

    acc_a = correct_a / total if total else 0.0
    acc_b = correct_b / total if total else 0.0
    print()
    print(f"评估 {total} 个窗口（耗时 {time.time() - t0:.0f}s，每窗口采样 {args.samples} 次，变体 {args.variant}）")
    actual_ups = sum(
        1 for i in split_points(df15, args.points, ctx)
        if close15[i] > close15[i - 1]
    )
    print(f"  窗口实际上涨比例: {actual_ups}/{total} = {actual_ups / total:.1%}")
    print()
    print("=" * 56)
    print(f"方案 A（15m 单步预测）: 准确率 {acc_a:.1%} ({correct_a}/{total})")
    print(f"方案 B（5m 三步预测）:  准确率 {acc_b:.1%} ({correct_b}/{total})")
    print("=" * 56)
    print(f"两方案方向分歧窗口: {disagreements}/{total}")
    print(f"A 平均 P(up): {sum(p_up_a_list) / len(p_up_a_list):.3f}   "
          f"B 平均 P(up): {sum(p_up_b_list) / len(p_up_b_list):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
