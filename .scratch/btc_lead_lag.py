"""探索：BTC 领先 ETH/SOL 的 lead-lag 量化（1m 粒度）。

5m 分析已证：BTC 信号方向命中率 ≈ ETH/SOL 自身信号（无方向 alpha），
且 BTC 穿越时 ETH/SOL 已穿越的窗口占 89-90%（领先窗口仅 10%）。

本脚本用 1m K线量化：BTC 穿越后，ETH/SOL 平均滞后多少分钟跟随穿越。
若滞后 <1 分钟 → 领先窗口太窄，难操作；若滞后 1-3 分钟 → 有操作价值。

用法：uv run python .scratch/btc_lead_lag.py [--days 30] [--x 0.08]
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.data_source import fetch_klines_batch

STEP_5M = 300_000
STEP_1M = 60_000
PAGE = 1000


def fetch_n(symbol: str, interval: str, days: int) -> list:
    now_ms = int(time.time() * 1000)
    step = STEP_1M if interval == "1m" else STEP_5M
    since = now_ms - days * 86400_000
    out: dict[int, object] = {}
    cur = since
    while cur < now_ms:
        try:
            batch = fetch_klines_batch(symbol, interval, cur, PAGE, proxies=None)
        except Exception as e:
            print(f"  {symbol} 拉取失败: {e}", file=sys.stderr)
            break
        if not batch:
            break
        for k in batch:
            out[k.timestamp] = k
        if len(batch) < PAGE:
            break
        cur = batch[-1].timestamp + step
    return sorted(out.values(), key=lambda k: k.timestamp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--x", type=float, default=0.08)
    args = ap.parse_args()

    print(f"=== 拉取 BTC/ETH/SOL 1m K线 {args.days} 天 ===")
    raw = {}
    for sym in ("BTC", "ETH", "SOL"):
        raw[sym] = fetch_n(sym, "1m", args.days)
        print(f"  {sym}: {len(raw[sym])} 根")

    # 按 5m 窗口分组：window -> [ (minute_idx 0-4, open, high, low, close) ]
    def group_5m(ks: list) -> dict[int, list]:
        wins: dict[int, list] = defaultdict(list)
        for k in ks:
            ws = k.timestamp - (k.timestamp % STEP_5M)
            minute_idx = (k.timestamp - ws) // STEP_1M
            wins[ws].append((minute_idx, k.open, k.high, k.low, k.close))
        return wins

    wins = {sym: group_5m(raw[sym]) for sym in ("BTC", "ETH", "SOL")}
    common = sorted(set(wins["BTC"]) & set(wins["ETH"]) & set(wins["SOL"]))
    # 只保留完整 5 根 1m bar 的窗口
    complete = [w for w in common if len(wins["BTC"][w]) == 5
                and len(wins["ETH"][w]) == 5 and len(wins["SOL"][w]) == 5]
    print(f"\n完整 5m 窗口数 = {len(complete)}")

    x = args.x

    def first_cross(seq: list, direction: str) -> int | None:
        """首次穿越 +x%/-x% 的分钟序号（0-4），未穿越返回 None。"""
        if not seq:
            return None
        o = seq[0][1]  # 第 0 分钟的 open ≈ 窗口 open
        for idx, _, hi, lo, _ in sorted(seq):
            if direction == "up" and hi >= o * (1 + x / 100):
                return idx
            if direction == "down" and lo <= o * (1 - x / 100):
                return idx
        return None

    # 用窗口内 min(open) 作为基准更稳（1m 第0根的 open 即窗口 open）
    def window_open(seq: list) -> float:
        return min(s[1] for s in seq)

    lag_up: list[int] = []
    lag_dn: list[int] = []
    both_cross_up = both_cross_dn = 0
    for w in complete:
        btc = wins["BTC"][w]
        btc_o = window_open(btc)
        # BTC 首次穿越分钟
        btc_up = first_cross(btc, "up")
        btc_dn = first_cross(btc, "down")
        for key in ("ETH", "SOL"):
            seq = wins[key][w]
            o = window_open(seq)
            e_up = first_cross(seq, "up")
            e_dn = first_cross(seq, "down")
            if btc_up is not None and e_up is not None:
                both_cross_up += 1
                lag_up.append(e_up - btc_up)  # 正=ETH/SOL 滞后，负=领先
            if btc_dn is not None and e_dn is not None:
                both_cross_dn += 1
                lag_dn.append(e_dn - btc_dn)

    import statistics
    print(f"\n=== BTC 领先 ETH/SOL 的分钟数（lag = 标的穿越分钟 - BTC 穿越分钟）===")
    for name, lags, n in (("涨穿 +X%", lag_up, both_cross_up),
                          ("跌穿 -X%", lag_dn, both_cross_dn)):
        if not lags:
            print(f"  {name}: 无样本")
            continue
        dist = defaultdict(int)
        for l in lags:
            dist[l] += 1
        lag0 = dist.get(0, 0)
        lag_ge1 = sum(v for k, v in dist.items() if k >= 1)
        lag_neg = sum(v for k, v in dist.items() if k < 0)
        print(f"  {name}: 样本 {n} 双穿越窗口")
        print(f"    同步(0min): {lag0} ({lag0/len(lags)*100:.0f}%)")
        print(f"    ETH/SOL 滞后 ≥1min: {lag_ge1} ({lag_ge1/len(lags)*100:.0f}%)")
        print(f"    ETH/SOL 领先(负): {lag_neg} ({lag_neg/len(lags)*100:.0f}%)")
        print(f"    平均 lag: {statistics.mean(lags):+.2f} 分钟  中位: {statistics.median(lags):+.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
