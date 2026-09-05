"""动量/breakout 策略回测:BTC 穿越阈值后同向入场。

关键发现:bid hit(BTC跌穿-X%)后 up_wins 仅19% → down_wins 81%(动量继续跌)
          ask hit(涨穿+X%)后 up_wins 80%(动量继续涨)
做市亏(adverse), 但反向——做动量(穿越后同向跟随)可能有 edge。
这是 path-dependent edge, 不是 opening-direction edge(Kronos 测的是后者, 50%)。
"""
from __future__ import annotations
import json, numpy as np
from collections import defaultdict

SNAP = "data/market_rec/snapshots_20260903.jsonl"
SETTLE = "data/market_rec/settlements.jsonl"
FEE = 0.03  # taker 双边


def load():
    snaps = [json.loads(l) for l in open(SNAP, encoding="utf-8")]
    rows = [json.loads(x) for x in open(SETTLE, encoding="utf-8")]
    settles = {s["window_start"]: s for s in rows if "up_price" in s}
    bw = defaultdict(list)
    for s in snaps:
        if s.get("binance"):
            bw[s["window_start"]].append(s)
    for w in bw:
        bw[w].sort(key=lambda x: x["ts"])
    return bw, settles


def main():
    bw, settles = load()
    valid = [w for w in bw if w in settles and len(bw[w]) >= 5]
    print(f"有效窗口 {len(valid)}")
    # 动量策略:BTC 跌穿 -X% → 买 down(跟随跌);涨穿 +X% → 买 up(跟随涨)
    # 入场价:假设 Polymarket 概率线性映射 BTC 偏离(k=100: 0.1%BTC=10%概率)
    # 跌穿 -X% 时 up 概率 = 0.5 - k*X/100, down 价 = 0.5 + k*X/100
    print(f"\n=== 动量策略(穿越后同向入场,扣3% taker费) ===")
    print(f"{'X%':>5} {'k':>4} {'触发率':>6} {'命中率':>7} {'入场价':>7} {'净EV/笔':>9} {'结论':>6}")
    for X in [0.03, 0.05, 0.08, 0.10, 0.15]:
        for k in [50, 100, 150]:
            trig = 0; hits = 0; tot = 0.0; prices = []
            for w in valid:
                up_wins = float(settles[w]["up_price"]) > 0.5
                pr = [s["binance"] for s in bw[w]]; op = pr[0]
                # 找首次穿越
                down_hit = None; up_hit = None
                for p in pr:
                    if down_hit is None and p <= op * (1 - X/100):
                        down_hit = p  # 跌穿, 买 down
                    if up_hit is None and p >= op * (1 + X/100):
                        up_hit = p  # 涨穿, 买 up
                if down_hit is not None and up_hit is None:  # 只跌穿(避免双触发歧义)
                    trig += 1
                    entry = min(0.95, 0.5 + k * X / 100)  # down 入场价
                    won = not up_wins  # 买 down, down 赢 = not up_wins
                    hits += won
                    prices.append(entry)
                    pnl = (1 - entry) * (1 - FEE) - entry * (1 + FEE) if won else -entry * (1 + FEE)
                    tot += pnl
                elif up_hit is not None and down_hit is None:  # 只涨穿, 买 up
                    trig += 1
                    entry = min(0.95, 0.5 + k * X / 100)  # up 入场价
                    won = up_wins
                    hits += won
                    prices.append(entry)
                    pnl = (1 - entry) * (1 - FEE) - entry * (1 + FEE) if won else -entry * (1 + FEE)
                    tot += pnl
            n = trig
            if n < 20:
                continue
            rate = hits / n
            avg_p = np.mean(prices)
            net = tot / n
            tag = "★★大正" if net > 0.05 else ("★正" if net > 0.01 else "")
            print(f"{X:>5.2f} {k:>4} {n/len(valid):>5.1%} {rate:>6.1%} {avg_p:>6.3f} {net:>+8.4f} {tag}")


if __name__ == "__main__":
    main()