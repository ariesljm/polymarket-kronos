"""找盈利方法:窗口内预测力随时间演化 + 期望收益。

核心问题:在窗口 t 秒处,用什么信号(盘口价/binance走势)预测结算 up_wins,
        准确率多少,对应净期望(扣3%费)是否为正。
"""
from __future__ import annotations
import json, math, statistics
from collections import defaultdict

SNAP = "data/market_rec/snapshots_20260903.jsonl"
SETTLE = "data/market_rec/settlements.jsonl"
FEE = 0.03
T_POINTS = [5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 280]


def load():
    snaps = [json.loads(l) for l in open(SNAP, encoding="utf-8")]
    rows = [json.loads(line) for line in open(SETTLE, encoding="utf-8")]
    settles = {s["window_start"]: s for s in rows if "up_price" in s}
    return snaps, settles


def quote_at(snaps_win, t, key="ask", side="up"):
    """窗口内 t 秒附近最近快照(±5s)的 up/down bid/ask。"""
    best = None
    for s in snaps_win:
        if abs((s["ts"] - snaps_win[0]["ts"]) - t) <= 5:
            d = s.get(side) or {}
            v = d.get(key)
            if v is not None:
                if best is None or abs((s["ts"]-snaps_win[0]["ts"])-t) < best[0]:
                    best = (abs((s["ts"]-snaps_win[0]["ts"])-t), v, s)
    return best[1] if best else None


def acc_and_ev(preds, settles, entry_fn):
    """preds: [(window, pred_up_bool)]。entry_fn(up_wins, entry_price)-> pnl。"""
    hits, pnls = 0, 0
    n = 0
    for w, pred_up in preds:
        if w not in settles:
            continue
        up_wins = float(settles[w]["up_price"]) > 0.5
        entry = entry_fn(w, pred_up)
        if entry is None:
            continue
        won = (pred_up == up_wins)
        hits += won
        size = 1.0 / entry
        cost = size * entry * (1 + FEE)
        proceeds = size * 1.0 * (1 - FEE) if won else 0
        pnls += proceeds - cost
        n += 1
    return (hits, n, pnls / n) if n else (0, 0, 0)


def main():
    snaps, settles = load()
    by_win = defaultdict(list)
    for s in snaps:
        if s.get("up") and s["up"].get("ask") is not None:
            by_win[s["window_start"]].append(s)
    for w in by_win:
        by_win[w].sort(key=lambda x: x["ts"])

    print(f"快照{len(snaps)} 窗口{len(by_win)} 有结算{len(settles)}")
    print(f"\n=== 核心分析:窗口 t 秒处预测结算 up_wins 的准确率+净期望 ===")
    print(f"{'t秒':>4} {'信号':>8} {'n':>4} {'命中':>6} {'准确率':>7} {'净期望/笔':>10} {'结论':>6}")
    for t in T_POINTS:
        # 信号1: t 秒时 up_ask(>0.5 预测 up)
        preds_ask, entries = [], {}
        for w, ws in by_win.items():
            if len(ws) < 2: continue
            a = quote_at(ws, t, "ask", "up")
            if a is None: continue
            preds_ask.append((w, a > 0.5))
            entries[(w, a > 0.5)] = a
        h, n, ev = acc_and_ev(preds_ask, settles, lambda w, pu: entries.get((w, pu)))
        acc = h / n if n else 0
        tag = "★正期望" if ev > 0.005 and n >= 30 else ""
        print(f"{t:>4} {'up_ask':>8} {n:>4} {h:>4}/{n} {acc:>6.1%} {ev:>+9.4f} {tag:>6}")

    print()
    # 信号2: t 秒时 binance 相对窗口开盘涨跌
    print(f"{'t秒':>4} {'信号':>8} {'n':>4} {'命中':>6} {'准确率':>7}")
    for t in T_POINTS:
        preds_b = []
        for w, ws in by_win.items():
            if len(ws) < 2: continue
            # 窗口开盘 binance(t=0 附近) vs t 秒 binance
            b0 = None
            for s in ws:
                if s.get("binance") and (s["ts"]-ws[0]["ts"]) <= 10:
                    b0 = s["binance"]; break
            bt = None
            for s in ws:
                if s.get("binance") and abs((s["ts"]-ws[0]["ts"])-t) <= 5:
                    bt = s["binance"]; break
            if b0 and bt:
                preds_b.append((w, bt > b0))
        h, n, _ = acc_and_ev(preds_b, settles, lambda w, pu: None)
        acc = h/n if n else 0
        print(f"{t:>4} {'binance':>8} {n:>4} {h:>4}/{n} {acc:>6.1%}")

    # 早期价差(可交易成本)
    print(f"\n=== 早期盘口价差(窗口前30秒) ===")
    early = []
    for w, ws in by_win.items():
        for s in ws:
            if (s["ts"]-ws[0]["ts"]) <= 30:
                u = s.get("up") or {}
                if u.get("ask") and u.get("bid"):
                    early.append(u["ask"]-u["bid"])
    if early:
        early.sort()
        print(f"价差: 中位{statistics.median(early):.3f} 均值{statistics.fmean(early):.3f} "
              f"薄(<0.04)占比{sum(1 for x in early if x<0.04)/len(early):.0%}")

    # 最佳策略:找净期望最高的 (t, 信号)
    print(f"\n=== 寻找最佳入场点(扣3%费后净期望) ===")
    best = (0, 0, 0, 0)
    for t in T_POINTS:
        preds, entries = [], {}
        for w, ws in by_win.items():
            if len(ws) < 2: continue
            a = quote_at(ws, t, "ask", "up")
            if a is None: continue
            preds.append((w, a > 0.5))
            entries[(w, a > 0.5)] = a
        h, n, ev = acc_and_ev(preds, settles, lambda w, pu: entries.get((w, pu)))
        if n >= 30 and ev > best[3]:
            best = (t, h, n, ev)
    t, h, n, ev = best
    if n:
        print(f"最佳: t={t}s up_ask信号 命中{h}/{n}={h/n:.1%} 净期望{ev:+.4f}/笔")
        print(f"→ {'有稳定盈利空间(需更多样本确认)' if ev>0.01 else '扣费后无正期望'}")


if __name__ == "__main__":
    main()