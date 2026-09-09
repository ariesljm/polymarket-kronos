"""验证：快速大动信号 vs 慢速穿越信号（命中率 + 触发频率）。

假设（chudi.dev 实测 + latency 套利理论）：
  - 慢信号（5m 窗口偏离 0.08%）：做市商 347ms 反映，检测到时盘口已极化，
    只有极窄窗口可入场（我们实测 3.7%）
  - 快信号（1 分钟移动 0.3%）：做市商滞后 30-90 秒，触发时盘口未充分反映，
    可稳定拿到 0.62-0.68 入场价

本脚本用 1m K线 30 天，对比两种信号的命中率（信号方向 == 5m 结算方向）
与触发频率，验证"快信号"是否更高确信、更值得作为入场依据。

用法：uv run python .scratch/fast_vs_slow_signal.py [--days 30]
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


def fetch_n(symbol: str, days: int) -> list:
    now_ms = int(time.time() * 1000)
    since = now_ms - days * 86400_000
    out: dict[int, object] = {}
    cur = since
    while cur < now_ms:
        try:
            batch = fetch_klines_batch(symbol, "1m", cur, PAGE, proxies=None)
        except Exception as e:
            print(f"  {symbol} 失败: {e}", file=sys.stderr)
            break
        if not batch:
            break
        for k in batch:
            out[k.timestamp] = k
        if len(batch) < PAGE:
            break
        cur = batch[-1].timestamp + STEP_1M
    return sorted(out.values(), key=lambda k: k.timestamp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    print(f"=== 拉取 1m K线 {args.days} 天 ===")
    raw = {}
    for sym in ("BTC", "ETH", "SOL"):
        raw[sym] = fetch_n(sym, args.days)
        print(f"  {sym}: {len(raw[sym])} 根")

    # 按 5m 窗口分组 1m bar
    def group(ks: list) -> dict[int, list]:
        wins: dict[int, list] = defaultdict(list)
        for k in ks:
            ws = k.timestamp - (k.timestamp % STEP_5M)
            wins[ws].append(k)
        return wins

    wins = {sym: group(raw[sym]) for sym in ("BTC", "ETH", "SOL")}
    common = sorted(set(wins["BTC"]) & set(wins["ETH"]) & set(wins["SOL"]))

    def win_open(bars: list) -> float:
        return min(b.open for b in bars)

    def win_close(bars: list) -> float:
        return max(b.close for b in bars)

    for sym in ("BTC", "ETH", "SOL"):
        print(f"\n=== {sym}：慢信号 vs 快信号 命中率（== 5m 结算方向）===")
        # 慢信号：5m 窗口 high/low 相对 open 偏离 x%
        # 快信号：任一 1m bar 的 |close-open|/open >= x%
        print(f"{'信号':36s} {'触发':>6s} {'命中率':>8s}")
        for slow_x in (0.08,):
            trig = hits = 0
            for w in common:
                bars = wins[sym][w]
                o = win_open(bars)
                c = win_close(bars)
                hi = max(b.high for b in bars)
                lo = min(b.low for b in bars)
                if hi >= o * (1 + slow_x / 100):
                    trig += 1; hits += c > o
                if lo <= o * (1 - slow_x / 100):
                    trig += 1; hits += c < o
            print(f"  慢: 窗口偏离 ±{slow_x}% {'':12s} {trig:6d} {hits/trig*100:7.1f}%")
        for fast_x in (0.15, 0.20, 0.30, 0.40):
            trig = hits = 0
            for w in common:
                bars = wins[sym][w]
                o = win_open(bars)
                c = win_close(bars)
                for b in bars:
                    move = (b.close - b.open) / b.open * 100 if b.open > 0 else 0
                    if move >= fast_x:
                        trig += 1; hits += c > o
                        break
                    if move <= -fast_x:
                        trig += 1; hits += c < o
                        break
            if trig:
                print(f"  快: 1m bar 移动 ±{fast_x}% {'':10s} {trig:6d} {hits/trig*100:7.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
