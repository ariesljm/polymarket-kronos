"""做市数学回测:挂在 BTC 偏离 ±X%,往返赚价差,单边靠结算。

用户洞察:盈利来自价格波动(往返),不是预测准确率。
做市核心:挂 bid+ask 双边,价格往返时对冲赚价差;单边时方向暴露靠结算。
假设 Polymarket up 概率 ≈ 线性映射 BTC 偏离(k=100: 0.1% BTC → 10% 概率)。
"""
from __future__ import annotations
import json, numpy as np, statistics
from collections import defaultdict

SNAP = "data/market_rec/snapshots_20260903.jsonl"
SETTLE = "data/market_rec/settlements.jsonl"
FEE_RATE, REBATE_SHARE = 0.07, 0.20


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


def rebate(p, cap=1.0):
    return (cap / p) * FEE_RATE * p * (1 - p) * REBATE_SHARE


def main():
    bw, settles = load()
    valid = [w for w in bw if w in settles and len(bw[w]) >= 5]
    print(f"有效窗口 {len(valid)}")
    # k: BTC偏离% → Polymarket概率偏离(线性映射系数)
    # 0.1% BTC → k*0.1% 概率。k=100 → 0.1%BTC=10%概率
    print(f"\n=== 做市回测(挂 bid/ask 在 BTC ±X%, Polymarket up 价 0.5∓Y) ===")
    print(f"{'X%':>5} {'Y(概率)':>7} {'往返率':>6} {'单边bid':>7} {'单边ask':>7} "
          f"{'净EV/窗':>9} {'+rebate':>8}")
    for X, k in [(0.03, 100), (0.05, 100), (0.08, 100), (0.10, 100), (0.15, 100), (0.05, 150), (0.05, 200)]:
        Y = X * k / 100  # 概率偏离(X% BTC × k → Y 概率)
        if Y >= 0.45: Y = 0.45
        bid_p = 0.5 - Y  # up bid 价(买 up)
        ask_p = 0.5 + Y  # up ask 价(卖 up)
        roundtrip = bid_only = ask_only = 0
        tot = 0.0
        for w in valid:
            up_wins = float(settles[w]["up_price"]) > 0.5
            prices = [s["binance"] for s in bw[w]]
            op = prices[0]
            bid_hit = any(p <= op * (1 - X/100) for p in prices)  # BTC 跌穿买价
            ask_hit = any(p >= op * (1 + X/100) for p in prices)  # BTC 涨穿卖价
            if bid_hit and ask_hit:
                roundtrip += 1
                tot += (ask_p - bid_p) + rebate(bid_p) + rebate(ask_p)  # 对冲赚价差+双rebate
            elif bid_hit:  # 只买 up,结算
                tot += (1 - bid_p if up_wins else -bid_p) + rebate(bid_p)
                bid_only += 1
            elif ask_hit:  # 只卖 up(=买 down @ ask_p),结算反算
                tot += (ask_p - 0 if up_wins else ask_p - 1) + rebate(ask_p)
                ask_only += 1
            # 都不触发:无成交
        n = len(valid)
        net = tot / n
        tag = "★正" if net > 0.005 else ""
        print(f"{X:>5.2f} {Y:>6.2f} {roundtrip/n:>5.1%} {bid_only/n:>6.1%} "
              f"{ask_only/n:>6.1%} {net:>+8.4f} {tag}")


if __name__ == "__main__":
    main()