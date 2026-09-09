"""回测：动量策略 + BTC 反向过滤 + 分标的 threshold 的 EV 对比。

验证结论（前序脚本已证）：
  - BTC 不领先 ETH/SOL（反向利用：BTC 反向穿越时标的命中率暴跌 41%）
  - 分标的 threshold：SOL 波动大需更高阈值

本脚本回测 30 天 5m，对比三种策略的触发数/命中率/EV：
  A. 基线（现状）：x=0.08 无过滤
  B. + BTC 反向过滤：x=0.08 剔除 BTC 反向窗口
  C. + 反向过滤 + 分标的 threshold（BTC 0.08 / ETH 0.10 / SOL 0.12）

EV 口径：Polymarket 真实费率（taker 买入费 0.07×p×(1-p)/股 ≈ 成交额 ~2%）+
        config 保守 3% 双边。入场价取盈亏平衡价对比稳健性。

用法：uv run python .scratch/backtest_filter.py [--days 30]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.data_source import fetch_klines_batch

STEP_5M = 300_000
PAGE = 1000
FEE = 0.03  # config 口径双边 3%


def fetch_n(symbol: str, days: int) -> list:
    now_ms = int(time.time() * 1000)
    since = now_ms - days * 86400_000
    out: dict[int, object] = {}
    cur = since
    while cur < now_ms:
        try:
            batch = fetch_klines_batch(symbol, "5m", cur, PAGE, proxies=None)
        except Exception as e:
            print(f"  {symbol} 失败: {e}", file=sys.stderr)
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


def breakeven(h: float) -> float:
    """盈亏平衡入场价：命中率 h 下，买入价低于此值为正 EV。"""
    return h * (1 - FEE) / (1 + FEE)


def ev(h: float, p: float) -> float:
    """每股 EV（双边费 FEE，赢收 1.0 输收 0）。"""
    return h * (1 - FEE) - p * (1 + FEE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    print(f"=== 回测 {args.days} 天 5m ===")
    wins = {}
    for sym in ("BTC", "ETH", "SOL"):
        wins[sym] = windows(fetch_n(sym, args.days))
    common = sorted(set(wins["BTC"]) & set(wins["ETH"]) & set(wins["SOL"]))
    print(f"共同窗口 {len(common)}\n")

    def cross(win: dict, x: float, side: str) -> bool:
        o = win["open"]
        return win["high"] >= o * (1 + x / 100) if side == "up" else win["low"] <= o * (1 - x / 100)

    def settle_up(win: dict) -> bool:
        return win["close"] > win["open"]

    # 三策略
    strategies = {
        "A 基线 x=0.08": {"BTC": 0.08, "ETH": 0.08, "SOL": 0.08, "filter": False},
        "B 反向过滤 x=0.08": {"BTC": 0.08, "ETH": 0.08, "SOL": 0.08, "filter": True},
        "C 反向过滤+分标的": {"BTC": 0.08, "ETH": 0.10, "SOL": 0.12, "filter": True},
    }

    print(f"{'策略':24s} {'标的':4s} {'触发':>6s} {'命中率':>7s} {'盈亏平衡价':>9s} {'EV@0.50':>9s}")
    print("-" * 70)
    for name, cfg in strategies.items():
        for sym in ("ETH", "SOL"):  # BTC 本身交易可行性另议，这里看 ETH/SOL
            x = cfg[sym]
            bx = cfg["BTC"]
            trig = hits = 0
            for w in common:
                d = wins[sym][w]
                o = d["open"]
                for side in ("up", "down"):
                    if not cross(d, x, side):
                        continue
                    # 反向过滤：标的穿越方向与 BTC 穿越方向相反
                    if cfg["filter"]:
                        btc = wins["BTC"][w]
                        if side == "up" and cross(btc, bx, "down"):
                            continue  # 标的涨穿但 BTC 跌穿 → 跳过
                        if side == "down" and cross(btc, bx, "up"):
                            continue  # 标的跌穿但 BTC 涨穿 → 跳过
                    trig += 1
                    hits += settle_up(d) if side == "up" else (not settle_up(d))
            if trig == 0:
                continue
            h = hits / trig
            print(f"{name:24s} {sym:4s} {trig:6d} {h*100:6.1f}% {breakeven(h):8.3f} {ev(h, 0.50):+8.3f}")
        print()

    # 敏感性：不同入场价下 EV（策略 B vs A）
    print("=== 入场价敏感性（EV/股，费率 3%）===")
    print(f"{'入场价':>8s} | {'ETH A':>8s} {'ETH B':>8s} | {'SOL A':>8s} {'SOL B':>8s} {'SOL C':>8s}")
    # 先算各策略命中率
    def hit_rate(sym, x, bx, flt):
        trig = hits = 0
        for w in common:
            d = wins[sym][w]
            o = d["open"]
            for side in ("up", "down"):
                if not cross(d, x, side):
                    continue
                if flt:
                    btc = wins["BTC"][w]
                    if side == "up" and cross(btc, bx, "down"):
                        continue
                    if side == "down" and cross(btc, bx, "up"):
                        continue
                trig += 1
                hits += settle_up(d) if side == "up" else (not settle_up(d))
        return hits / trig if trig else 0
    rates = {
        "ETH A": hit_rate("ETH", 0.08, 0.08, False),
        "ETH B": hit_rate("ETH", 0.08, 0.08, True),
        "SOL A": hit_rate("SOL", 0.08, 0.08, False),
        "SOL B": hit_rate("SOL", 0.08, 0.08, True),
        "SOL C": hit_rate("SOL", 0.12, 0.08, True),
    }
    for p in (0.30, 0.40, 0.50, 0.60, 0.70):
        print(f"{p:8.2f} | {ev(rates['ETH A'], p):+8.3f} {ev(rates['ETH B'], p):+8.3f} | "
              f"{ev(rates['SOL A'], p):+8.3f} {ev(rates['SOL B'], p):+8.3f} {ev(rates['SOL C'], p):+8.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
