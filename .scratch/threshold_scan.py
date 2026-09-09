"""探索：分标的 threshold 扫描 + 多标的方向确认/矛盾过滤。

已证（前两个脚本）：BTC 不领先 ETH/SOL（平均 lag -0.45min），
BTC 交叉信号无方向 alpha。

本脚本探索真正可操作的优化：
  1. 分标的 threshold 扫描：SOL 波动 > ETH > BTC，同样 0.08% 下命中率不同，
     各自最优 threshold 可能不同
  2. 确认信号：标的穿越时 BTC 同向已穿越 → 命中率是否更高
  3. 矛盾过滤：标的穿越时 BTC 反向 → 命中率是否更低（可用于过滤）

用法：uv run python .scratch/threshold_scan.py [--days 30]
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
PAGE = 1000


def fetch_n(symbol: str, days: int) -> list:
    now_ms = int(time.time() * 1000)
    since = now_ms - days * 86400_000
    out: dict[int, object] = {}
    cur = since
    while cur < now_ms:
        try:
            batch = fetch_klines_batch(symbol, "5m", cur, PAGE, proxies=None)
        except Exception as e:
            print(f"  {symbol} 拉取失败: {e}", file=sys.stderr)
            break
        if not batch:
            break
        for k in batch:
            out[k.timestamp] = k
        if len(batch) < PAGE:
            break
        cur = batch[-1].timestamp + STEP_5M
    return sorted(out.values(), key=lambda k: k.timestamp)


def windows(ks: list) -> dict[int, dict]:
    out = {}
    for k in ks:
        ws = k.timestamp - (k.timestamp % STEP_5M)
        out[ws] = {"open": k.open, "close": k.close, "high": k.high, "low": k.low}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    print(f"=== 拉取 5m K线 {args.days} 天 ===")
    wins = {}
    for sym in ("BTC", "ETH", "SOL"):
        wins[sym] = windows(fetch_n(sym, args.days))
        print(f"  {sym}: {len(wins[sym])} 窗")
    common = sorted(set(wins["BTC"]) & set(wins["ETH"]) & set(wins["SOL"]))
    print(f"共同窗口 {len(common)}")

    # 1. 分标的 threshold 扫描
    print("\n=== 1. 分标的 threshold 扫描（命中率 = 穿越方向==结算方向）===")
    print(f"{'X%':>6} | {'BTC 触发':>8} {'BTC命中':>7} | {'ETH 触发':>8} {'ETH命中':>7} | {'SOL 触发':>8} {'SOL命中':>7}")
    for x in (0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20):
        row = [f"{x:5.2f}"]
        for sym in ("BTC", "ETH", "SOL"):
            d = wins[sym]
            trig = hits = 0
            for w in common:
                o = d[w]["open"]
                up = d[w]["high"] >= o * (1 + x / 100)
                dn = d[w]["low"] <= o * (1 - x / 100)
                if up:
                    trig += 1
                    hits += d[w]["close"] > o
                if dn:
                    trig += 1
                    hits += d[w]["close"] < o
            row.append(f"{trig:8d} {hits/trig*100:6.1f}%" if trig else f"{trig:8d} {'--':>7}")
        print(" | ".join(row))

    # 2. 确认信号：标的穿越时，BTC 是否同向已穿越
    x = 0.08
    print(f"\n=== 2. 确认信号（x={x}%）：标的穿越时 BTC 同向状态 → 标的命中率 ===")
    for key in ("ETH", "SOL"):
        d = wins[key]
        grp = defaultdict(lambda: [0, 0])  # btc_state -> [hit, total]
        for w in common:
            o = d[w]["open"]
            bo = wins["BTC"][w]["open"]
            # 涨穿
            if d[w]["high"] >= o * (1 + x / 100):
                btc_up = wins["BTC"][w]["high"] >= bo * (1 + x / 100)
                btc_dn = wins["BTC"][w]["low"] <= bo * (1 - x / 100)
                state = "BTC同向涨" if btc_up else ("BTC反向跌" if btc_dn else "BTC未动")
                grp[state][1] += 1
                grp[state][0] += d[w]["close"] > o
            if d[w]["low"] <= o * (1 - x / 100):
                btc_up = wins["BTC"][w]["high"] >= bo * (1 + x / 100)
                btc_dn = wins["BTC"][w]["low"] <= bo * (1 - x / 100)
                state = "BTC同向跌" if btc_dn else ("BTC反向涨" if btc_up else "BTC未动")
                grp[state][1] += 1
                grp[state][0] += d[w]["close"] < o
        print(f"  {key}:")
        for state in ("BTC同向涨", "BTC同向跌", "BTC未动", "BTC反向涨", "BTC反向跌"):
            h, t = grp.get(state, [0, 0])
            if t:
                print(f"    {state:10s}: {h}/{t} = {h/t*100:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
