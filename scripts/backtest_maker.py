"""Maker 做市策略回测:验证 adverse selection 后的净期望。

费率真相(已调研):
- maker 费 = 0,rebate = 20% of fee_equivalent(crypto feeRate=0.07)
- fee_equivalent = C * 0.07 * p * (1-p),p=0.5 时最大
- 单笔 1 USDC @ p=0.5: shares=2, fee_eq=2*0.07*0.25=0.035, rebate=20%*0.035=0.007 (0.7%)

策略:每窗口挂 maker bid @ B(买 up),成交条件=窗口内 up_ask 曾 <= B
     (ask 跌穿我的买价 -> taker sell hit 我的 bid)。
     成交后持有到结算。测 P(up_wins|成交) 与净期望。
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
    rows = [json.loads(line) for line in open(SETTLE, encoding="utf-8")]
    settles = {s["window_start"]: s for s in rows if "up_price" in s}
    by_win = defaultdict(list)
    for s in snaps:
        if s.get("up") and s["up"].get("ask") is not None and s.get("binance"):
            by_win[s["window_start"]].append(s)
    for w in by_win:
        by_win[w].sort(key=lambda x: x["ts"])
    return by_win, settles


def rebate_per_trade(entry, capital=1.0):
    """单笔 maker rebate(USDC),1 USDC 投入。"""
    shares = capital / entry
    fee_eq = shares * FEE_RATE * entry * (1 - entry)
    return fee_eq * REBATE_SHARE


def main():
    by_win, settles = load()
    print(f"窗口{len(by_win)} 有结算{len(settles)}")
    print(f"\n=== 单边 maker bid(买 up,持有到结算) ===")
    print(f"{'bid价':>6} {'成交率':>7} {'命中':>6} {'P(win)':>7} "
          f"{'毛期望':>8} {'rebate':>7} {'净期望':>8} {'结论':>6}")
    for B in [0.40, 0.45, 0.48, 0.50, 0.52, 0.55, 0.60]:
        fills, wins, pnls = 0, 0, 0.0
        for w, ws in by_win.items():
            if w not in settles: continue
            up_wins = float(settles[w]["up_price"]) > 0.5
            # 成交 = 窗口内 up_ask 曾 <= B
            hit = any(s["up"]["ask"] <= B for s in ws)
            if not hit: continue
            fills += 1
            won = up_wins
            wins += won
            pnl = (1.0 - B if won else -B) + rebate_per_trade(B)
            pnls += pnl
        n = fills
        pwin = wins / n if n else 0
        fill_rate = n / len([w for w in by_win if w in settles])
        gross = pwin * (1 - B) - (1 - pwin) * B
        reb = rebate_per_trade(B)
        net = pnls / n if n else 0
        tag = "★正" if net > 0.002 and n >= 30 else ""
        print(f"{B:>6.2f} {fill_rate:>6.1%} {wins:>3}/{n:<3} {pwin:>6.1%} "
              f"{gross:>+7.4f} {reb:>6.4f} {net:>+7.4f} {tag:>6}")

    print(f"\n=== 双边做市(bid B + ask A,对冲) ===")
    print(f"{'bid':>5}{'ask':>5} {'bid成交':>7} {'ask成交':>7} {'双成交':>7} "
          f"{'净PnL/窗':>9} {'说明':>10}")
    for B, A in [(0.49, 0.51), (0.48, 0.52), (0.47, 0.53), (0.45, 0.55)]:
        bid_fills = ask_fills = both = 0
        total_pnl = 0.0
        for w, ws in by_win.items():
            if w not in settles: continue
            up_wins = float(settles[w]["up_price"]) > 0.5
            bid_hit = any(s["up"]["ask"] <= B for s in ws)  # 价格跌到 bid
            ask_hit = any(s["up"].get("bid") and s["up"]["bid"] >= A for s in ws)  # 价格涨到 ask
            bid_fills += bid_hit
            ask_fills += ask_hit
            if bid_hit and ask_hit:
                both += 1
                # 对冲:赚价差 A-B + 双边 rebate,无方向暴露
                total_pnl += (A - B) + rebate_per_trade(B) + rebate_per_trade(A)
            elif bid_hit:  # 只买进 up,持有结算
                total_pnl += (1.0 - B if up_wins else -B) + rebate_per_trade(B)
            elif ask_hit:  # 只卖 up(短),结算反算
                total_pnl += (A - 0.0 if up_wins else A - 1.0) + rebate_per_trade(A)
        n = len([w for w in by_win if w in settles])
        print(f"{B:>5.2f}{A:>5.2f} {bid_fills/n:>6.1%} {ask_fills/n:>6.1%} "
              f"{both/n:>6.1%} {total_pnl/n:>+8.4f} "
              f"{'★对冲率高' if both/n>0.5 else '':>10}")


if __name__ == "__main__":
    main()