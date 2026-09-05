"""分析记录器数据:结算分布 + Binance 领先 + 价格收敛 + 价差。

修正:up_wins = float(up_price) > 0.5(原 == "1" 在未归一时误判)。
"""
from __future__ import annotations
import json, math, statistics
from collections import defaultdict

SNAP = "data/market_rec/snapshots_20260903.jsonl"
SETTLE = "data/market_rec/settlements.jsonl"


def load():
    snaps = [json.loads(l) for l in open(SNAP, encoding="utf-8")]
    settles = [json.loads(l) for l in open(SETTLE, encoding="utf-8")
               if "up_price" in l]
    return snaps, settles


def binom_test(ups, n):
    """二项检验 p=0.5。"""
    z = (ups - n / 2) / math.sqrt(n / 4)
    p = ups / n
    lo = p - 1.96 * math.sqrt(p * (1 - p) / n)
    hi = p + 1.96 * math.sqrt(p * (1 - p) / n)
    return p, z, lo, hi


def cross_corr(b_ret, u_ret, max_lag):
    """互相关:corr(b_ret[t], u_ret[t+lag])。lag>0 = binance 领先。"""
    n = len(b_ret)
    out = []
    for lag in range(-max_lag, max_lag + 1):
        xs, ys = [], []
        for t in range(max(0, -lag), min(n, n - lag)):
            xs.append(b_ret[t])
            ys.append(u_ret[t + lag])
        if len(xs) > 10:
            c = statistics.correlation(xs, ys) if len(set(xs)) > 1 else 0
            out.append((lag * 5, c, len(xs)))  # lag*5 秒
    return out


def main():
    snaps, settles = load()
    print(f"快照 {len(snaps)} 条, 结算(含up_price) {len(settles)} 条")

    # === A. 结算分布(修正) ===
    up_wins = sum(1 for s in settles if float(s["up_price"]) > 0.5)
    n = len(settles)
    p, z, lo, hi = binom_test(up_wins, n)
    print(f"\n=== A. 结算分布(修正) ===")
    print(f"up={up_wins} down={n-up_wins} up率={p:.1%} z={z:+.2f} 95%CI[{lo:.1%},{hi:.1%}]")
    print(f"方向 50/50? {'是(随机游走)' if abs(z)<1.96 else '否(显著偏离)'}")

    # === B. Binance 领先检验 ===
    # 全局时间序列(跨窗口连续),binance 5s return vs up_ask 5s return
    valid = [s for s in snaps if s.get("up") and s["up"].get("ask") is not None
             and s.get("binance")]
    valid.sort(key=lambda s: s["ts"])
    print(f"\n=== B. Binance 领先检验(5s 粒度,{len(valid)}有效快照) ===")
    # 计算 returns(相邻快照)
    b_ret, u_ret = [], []
    for i in range(1, len(valid)):
        b0, b1 = valid[i-1]["binance"], valid[i]["binance"]
        u0, u1 = valid[i-1]["up"]["ask"], valid[i]["up"]["ask"]
        if b0 and u0 and u0 > 0.01:  # 跳过极化价(ask 消失前)
            b_ret.append((b1 - b0) / b0)
            u_ret.append((u1 - u0) / u0)
    print(f"return 对数 {len(b_ret)}")
    cc = cross_corr(b_ret, u_ret, max_lag=12)  # ±60 秒
    best = max(cc, key=lambda x: abs(x[1]))
    print(f"互相关峰值: lag={best[0]:+d}s corr={best[1]:+.3f} (n={best[2]})")
    for lag, c, _ in cc[::2]:  # 每 10 秒一行
        print(f"  lag {lag:+3d}s: {c:+.3f}{' <==峰值' if (lag,c)==(best[0],best[1]) else ''}")
    lead = best[0]
    if best[1] > 0.1 and lead > 0:
        print(f"→ Binance 领先 Polymarket 约 {lead}秒(corr={best[1]:+.3f}),可套利!")
    elif best[1] > 0.3 and lead <= 0:
        print(f"→ 同步(lag<=0),Polymarket 跟 Binance 无延迟,无套利窗口")
    else:
        print(f"→ 相关弱({best[1]:+.3f}),或 5s 粒度测不到更短延迟")

    # === C. 价格收敛速度 ===
    print(f"\n=== C. 价格收敛速度 ===")
    by_win = defaultdict(list)
    for s in valid:
        by_win[s["window_start"]].append(s)
    conv_times = []
    for w, ws in by_win.items():
        ws.sort(key=lambda x: x["ts"])
        for s in ws:
            a = s["up"]["ask"]
            if a >= 0.8 or a <= 0.2:
                conv_times.append(s["ts"] - w)
                break
    if conv_times:
        conv_times.sort()
        md = statistics.median(conv_times)
        p25 = conv_times[len(conv_times)//4]
        p75 = conv_times[len(conv_times)*3//4]
        print(f"达到 >0.8/<0.2 的窗口 {len(conv_times)}/{len(by_win)}")
        print(f"收敛耗时: 中位{md}s  P25={p25}s  P75={p75}s  最快{conv_times[0]}s 最慢{conv_times[-1]}s")
        print(f"→ {'信息秒级反映(EMH成立,无残差套利)' if md<30 else '收敛慢,存在残差窗口'}")

    # === D. 价差分布 ===
    print(f"\n=== D. 盘口价差(up_ask-up_bid) ===")
    spreads = []
    for s in valid:
        u = s.get("up")
        if u and u.get("ask") and u.get("bid"):
            spreads.append(u["ask"] - u["bid"])
    if spreads:
        spreads.sort()
        print(f"价差样本 {len(spreads)}: 中位={statistics.median(spreads):.3f} "
              f"P25={spreads[len(spreads)//4]:.3f} P75={spreads[len(spreads)*3//4]:.3f} "
              f"均值={statistics.fmean(spreads):.3f}")
        thin = sum(1 for x in spreads if x < 0.02)
        print(f"价差<0.02(薄): {thin}/{len(spreads)}={thin/len(spreads):.0%}")


if __name__ == "__main__":
    main()