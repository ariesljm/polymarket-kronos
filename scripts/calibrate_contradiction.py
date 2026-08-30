"""方向一致性过滤（contradiction_skip_pct）阈值标定脚本。

对每条已评估预测，拉窗口起点后 300 秒的 1s K 线（Binance 镜像，缓存本地），
重建「入场延迟 T 秒时的实时移动」delta_T = (price_at_T - baseline)/baseline。
枚举入场延迟 T（10/30/60/120s，对齐真实 tick 与阈值等待）与过滤阈值 X，
统计：被过滤样本中模型方向错误率（correct=0）vs 未过滤样本 ——
若过滤样本坏单率显著更高，说明过滤避开了烂单，X 存在正向价值。

1s K 线粒度局限：K 线 open 是秒级价格，足够近似入场时刻实时价。

用法: uv run python -m scripts.calibrate_contradiction [--skip 0.1 0.3 0.5]
"""

from __future__ import annotations

import argparse
import time

import pandas as pd

from pmbot.data_source import fetch_klines_batch

DATA_DIR = "data"
CACHE = f"{DATA_DIR}/recordings/calib_1s.csv"
WINDOW_SEC = 300
PROXIES = {"http": None, "https": None}  # Binance 镜像直连（实测可达）


def load_1s_path(ts_ms: int) -> pd.DataFrame | None:
    """窗口起点后 300s 的 1s K 线（CSV 缓存命中直接读；单窗一次请求）。"""
    import os

    start = ts_ms
    cache = pd.DataFrame()
    if os.path.exists(CACHE):
        cache = pd.read_csv(CACHE)
    hit = cache[cache["start_ts"] == start] if not cache.empty else pd.DataFrame()
    if not hit.empty:
        return hit
    for attempt in range(2):  # 失败重试一次（网络抖动）
        try:
            kls = fetch_klines_batch("BTC", "1s", since=start, limit=WINDOW_SEC,
                                     proxies=PROXIES)
            if not kls:
                return None
            rows = pd.DataFrame([{
                "start_ts": start, "t": k.timestamp, "price": k.open,
            } for k in kls])
            cache = pd.concat([cache, rows], ignore_index=True)
            cache.to_csv(CACHE, index=False)
            return rows
        except Exception:
            time.sleep(1.0)
    return None


def price_at(path: pd.DataFrame, t_sec: int) -> float | None:
    """窗口起点后 t 秒的价格近似（1s K 线 open；缺档取最接近时刻）。"""
    if path is None or path.empty:
        return None
    ts = path["t"]
    target = ts.iloc[0] + t_sec * 1000
    diff = (ts - target).abs()
    idx = diff.idxmin()
    if diff[idx] > 2000:  # 缺失过多不近似
        return None
    return float(path.loc[idx, "price"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip", nargs="*", type=float, default=[0.1, 0.2, 0.3, 0.5, 0.8, 1.0])
    ap.add_argument("--delay", nargs="*", type=int, default=[10, 30, 60, 120])
    args = ap.parse_args()

    preds = pd.read_csv(f"{DATA_DIR}/predictions_btc.csv")
    klines = pd.read_csv(f"{DATA_DIR}/btc_5m.csv")
    df = preds[(preds.evaluated == 1) & (preds.ts.isin(klines.timestamp))].copy()
    df["dir_up"] = df["direction"] == "up"

    print(f"样本: {len(df)} 条（命中窗口起点）; 模型总准确率 {df.correct.mean()*100:.1f}%")
    print(f"拉取 1s 价格路径（缓存 {CACHE}）...")
    df["path"] = df["ts"].apply(load_1s_path)
    ok = df["path"].notna()
    print(f"路径可用: {ok.sum()}/{len(df)}")

    for T in args.delay:
        print(f"\n===== 入场延迟 T={T}s =====")
        df[f"delta_{T}"] = df.apply(
            lambda r: None if r["path"] is None else
            (price_at(r["path"], T) - r["baseline_close"]) / r["baseline_close"] * 100.0,
            axis=1)
        d = df[(df[f"delta_{T}"].notna())].copy()
        print(f"{'阈值%':>6} {'过滤':>4} {'过滤坏单率':>10} {'未过滤坏单率':>12} {'净值':>5}")
        for x in args.skip:
            mask = (d["dir_up"] & (d[f"delta_{T}"] <= -x)) | \
                   (~d["dir_up"] & (d[f"delta_{T}"] >= x))
            f, kp = d[mask], d[~mask]
            if len(f) == 0:
                print(f"{x:>6} {'0':>4} {'-':>10} {(1-kp.correct).mean()*100:>11.1f}%")
                continue
            net = int((f.correct == 0).sum() - (f.correct == 1).sum())
            print(f"{x:>6} {len(f):>4} {(1-f.correct).mean()*100:>9.1f}% "
                  f"{(1-kp.correct).mean()*100:>11.1f}% {net:+d}")

    print("\np_up 分档准确率:")
    df2 = df.dropna(subset=["path"]).copy()
    df2["p_up_bin"] = pd.cut(df2.p_up, bins=[0, 0.4, 0.5, 0.6, 0.7, 1.0],
                             labels=["≤0.4", "0.4-0.5", "0.5-0.6", "0.6-0.7", ">0.7"])
    for b, row in df2.groupby("p_up_bin", observed=True).agg(
            n=("correct", "size"), acc=("correct", "mean")).iterrows():
        print(f"  {b}: {int(row.n):>3} 条, 准确率 {row.acc*100:5.1f}%")


if __name__ == "__main__":
    main()