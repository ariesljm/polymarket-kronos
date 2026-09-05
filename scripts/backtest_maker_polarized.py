"""极化区 maker 做市回测 + rebate/rewards 净期望。

5m 窗口 16秒后价格极化到 0.8-0.99,停留约 280 秒。maker 实际挂单在极化区。
测:bid/ask 在 0.80-0.98,成交(价格穿越挂价)、P(win|成交)、净期望(含 rebate)。
adverse selection = P(win|bid成交) 应 < 公允 up_ask(被 informed taker 吃)。
"""
from __future__ import annotations
import json, math, statistics
from collections import defaultdict

SNAP = "data/market_rec/snapshots_20260903.jsonl"
SETTLE = "data/market_rec/settlements.jsonl"
FEE_RATE = 0.07
REBATE_SHARE = 0.20


def load():
    snaps = [json.loads(l) for l in open(SNAP, encoding="utf-8")]
    rows = [json.loads(x) for x in open(SETTLE, encoding="utf-8")]
    settles = {s["window_start"]: s for s in rows if "up_price" in s}
    by_win = defaultdict(list)
    for s in snaps:
        if s.get("up") and s["up"].get("ask") is not None:
            by_win[s["window_start"]].append(s)
    for w in by_win:
        by_win[w].sort(key=lambda x: x["ts"])
    return by_win, settles


def rebate(entry, cap=1.0):
    shares = cap / entry
    return shares * FEE_RATE * entry * (1 - entry) * REBATE_SHARE


def main():
    by_win, settles = load()
    wins_settled = [w for w in by_win if w in settles]
    print(f"窗口{len(by_win)} 有结算{len(wins_settled)}")
    print(f"\n=== 极化区单边 maker bid(买 up,持有结算) ===")
    print(f"{'bid':>5} {'成交率':>6} {'P(win)':>7} {'公允ask':>7} {'adverse':>7} "
          f"{'毛EV':>7} {'rebate':>6} {'净EV':>7}")
    for B in [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.92, 0.95, 0.98]:
        fills, wins, fair_asks = 0, 0, []
        for w in wins_settled:
            up_wins = float(settles[w]["up_price"]) > 0.5
            ws = by_win[w]
            # 成交=窗口内 up_ask 曾 <= B(价格跌穿我的买价)
            if any(s["up"]["ask"] <= B for s in ws):
                fills += 1
                wins += up_wins
                # 成交时刻的公允 ask(首次<=B 时的 ask)
                for s in ws:
                    if s["up"]["ask"] <= B:
                        fair_asks.append(s["up"]["ask"]); break
        n = fills
        if n == 0:
            print(f"{B:>5.2f} {0:>6.1%} {'-':>7}")
            continue
        pwin = wins / n
        fair = statistics.fmean(fair_asks) if fair_asks else B
        adverse = pwin - (1 - B)  # P(win) - 公允(1-B=赢概率公允)
        gross = pwin * (1 - B) - (1 - pwin) * B
        reb = rebate(B)
        net = gross + reb
        tag = "★" if net > 0.003 else ""
        print(f"{B:>5.2f} {n/len(wins_settled):>5.1%} {pwin:>6.1%} {1-B:>6.1%} "
              f"{adverse:>+6.1%} {gross:>+6.4f} {reb:>5.4f} {net:>+6.4f} {tag}")

    print(f"\n=== 双边做市(bid B + ask A,对冲) ===")
    print(f"{'B':>5}{'A':>5} {'bid成':>5} {'ask成':>5} {'双成':>5} "
          f"{'净EV/窗':>8} {'说明':>8}")
    for B, A in [(0.80, 0.95), (0.85, 0.92), (0.88, 0.95), (0.90, 0.96), (0.85, 0.98)]:
        bf = af = both = 0
        tot = 0.0
        for w in wins_settled:
            up_wins = float(settles[w]["up_price"]) > 0.5
            ws = by_win[w]
            bid_hit = any(s["up"]["ask"] <= B for s in ws)
            ask_hit = any(s["up"].get("bid") and s["up"]["bid"] >= A for s in ws)
            bf += bid_hit; af += ask_hit
            if bid_hit and ask_hit:
                both += 1
                tot += (A - B) + rebate(B) + rebate(A)  # 对冲,赚价差+双rebate
            elif bid_hit:
                tot += (1 - B if up_wins else -B) + rebate(B)
            elif ask_hit:
                tot += (A if up_wins else A - 1) + rebate(A)
        n = len(wins_settled)
        print(f"{B:>5.2f}{A:>5.2f} {bf/n:>4.0%} {af/n:>4.0%} {both/n:>4.0%} "
              f"{tot/n:>+7.4f} {'对冲多' if both/n>0.4 else '方向暴露大':>8}")


if __name__ == "__main__":
    main()