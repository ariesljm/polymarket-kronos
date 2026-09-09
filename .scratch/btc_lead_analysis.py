"""探索：BTC 作为标杆信号，对 ETH/SOL 的跨币种方向传导是否有 alpha。

验证问题（用户假设）：BTC 是加密货币标杆，其它币与其相关性高，
用 BTC 的动量穿越信号指导 ETH/SOL 交易，是否比 ETH/SOL 自身信号更优？

数据：Binance 5m K线 30 天（data-api.binance.vision）。
方法：
  1. 方向相关性：BTC 窗口结算方向 vs ETH/SOL 结算方向的一致性
  2. 穿越传导：BTC 穿越 ±X% 的窗口，ETH/SOL 结算方向是否跟随 BTC
  3. 交叉信号对比：BTC 信号预测 ETH/SOL vs ETH/SOL 自身信号

纯分析脚本，不改项目代码。
用法：uv run python .scratch/btc_lead_analysis.py [--days 30] [--x 0.08]
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


def fetch_30d(symbol: str, interval: str = "5m", days: int = 30) -> list:
    """翻页拉取最近 N 天 K 线。"""
    now_ms = int(time.time() * 1000)
    step = STEP_5M if interval == "5m" else 60_000
    since = now_ms - days * 86400_000
    out: dict[int, object] = {}
    # 从 since 开始向后翻页
    cur = since
    while cur < now_ms:
        try:
            batch = fetch_klines_batch(symbol, interval, cur, PAGE, proxies=None)
        except Exception as e:
            print(f"  {symbol} 拉取失败 since={cur}: {e}", file=sys.stderr)
            break
        if not batch:
            break
        for k in batch:
            out[k.timestamp] = k
        if len(batch) < PAGE:
            break
        cur = batch[-1].timestamp + step
    ks = sorted(out.values(), key=lambda k: k.timestamp)
    print(f"  {symbol}: {len(ks)} 根 {interval} K线 "
          f"[{time.strftime('%m-%d %H:%M', time.gmtime(ks[0].timestamp/1000))} ~ "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(ks[-1].timestamp/1000))}]")
    return ks


def align_windows(ks: list) -> dict[int, dict]:
    """按 5m 窗口对齐：window_start -> {open, close, high, low}。"""
    wins: dict[int, dict] = {}
    for k in ks:
        ws = k.timestamp - (k.timestamp % STEP_5M)
        wins[ws] = {"open": k.open, "close": k.close, "high": k.high, "low": k.low}
    return wins


def crossed(win: dict, x: float) -> tuple[bool, bool]:
    """窗口内是否曾涨穿 +x% / 跌穿 -x%（相对 open）。"""
    o = win["open"]
    return (win["high"] >= o * (1 + x / 100), win["low"] <= o * (1 - x / 100))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--x", type=float, default=0.08)
    args = ap.parse_args()

    print(f"=== 拉取 BTC/ETH/SOL 5m K线 {args.days} 天 ===")
    data = {}
    for sym in ("BTC", "ETH", "SOL"):
        data[sym] = align_windows(fetch_30d(sym, "5m", args.days))

    # 三标的共同窗口（对齐）
    common = sorted(set(data["BTC"]) & set(data["ETH"]) & set(data["SOL"]))
    print(f"\n共同 5m 窗口数 = {len(common)}")

    x = args.x

    # 分析 1：方向相关性（无条件）
    agree = {"ETH": 0, "SOL": 0}
    tot = 0
    for w in common:
        btc_dir = 1 if data["BTC"][w]["close"] > data["BTC"][w]["open"] else -1
        for sym, key in (("ETH", "ETH"), ("SOL", "SOL")):
            d = data[sym][w]
            sdir = 1 if d["close"] > d["open"] else -1
            if btc_dir == sdir:
                agree[key] += 1
        tot += 1
    print("\n=== 分析 1：无条件方向相关性（BTC 窗口方向 → ETH/SOL 方向）===")
    for key in ("ETH", "SOL"):
        print(f"  BTC→{key} 同向: {agree[key]}/{tot} = {agree[key]/tot*100:.1f}%")

    # 分析 2：BTC 穿越传导
    print(f"\n=== 分析 2：BTC 穿越 ±{x}% 后，ETH/SOL 结算方向跟随率 ===")
    for side, cond in (("涨穿 +X%", lambda win: win["high"] >= win["open"]*(1+x/100)),
                       ("跌穿 -X%", lambda win: win["low"] <= win["open"]*(1-x/100))):
        btc_side = [w for w in common if cond(data["BTC"][w])]
        if not btc_side:
            print(f"  {side}: 无样本")
            continue
        for key in ("ETH", "SOL"):
            follow = sum(
                1 for w in btc_side
                if (data[key][w]["close"] > data[key][w]["open"]) == (side.startswith("涨"))
            )
            print(f"  BTC {side} ({len(btc_side)}窗) → {key} 跟随 {follow}/{len(btc_side)} = {follow/len(btc_side)*100:.1f}%")

    # 分析 3：交叉信号 vs 自身信号（命中率对比）
    print(f"\n=== 分析 3：信号命中率对比（穿越方向 == 结算方向）===")
    for key in ("ETH", "SOL"):
        win = data[key]
        # 自身信号：ETH 穿越 +X% → ETH 结算涨
        up_trig = [w for w in common if win[w]["high"] >= win[w]["open"]*(1+x/100)]
        dn_trig = [w for w in common if win[w]["low"] <= win[w]["open"]*(1-x/100)]
        self_hit = 0
        self_tot = 0
        for w in up_trig:
            self_tot += 1
            self_hit += win[w]["close"] > win[w]["open"]
        for w in dn_trig:
            self_tot += 1
            self_hit += win[w]["close"] < win[w]["open"]
        # BTC 交叉信号：BTC 穿越 → 预测 ETH 同向
        btc_up = [w for w in common if data["BTC"][w]["high"] >= data["BTC"][w]["open"]*(1+x/100)]
        btc_dn = [w for w in common if data["BTC"][w]["low"] <= data["BTC"][w]["open"]*(1-x/100)]
        cross_hit = 0
        cross_tot = 0
        for w in btc_up:
            cross_tot += 1
            cross_hit += win[w]["close"] > win[w]["open"]
        for w in btc_dn:
            cross_tot += 1
            cross_hit += win[w]["close"] < win[w]["open"]
        print(f"  {key}: 自身信号 {self_hit}/{self_tot}={self_hit/self_tot*100:.1f}%  |  "
              f"BTC交叉信号 {cross_hit}/{cross_tot}={cross_hit/cross_tot*100:.1f}%")

    # 分析 4：BTC 穿越但 ETH/SOL 尚未穿越的窗口（潜在领先窗口）
    print(f"\n=== 分析 4：BTC 穿越时 ETH/SOL 是否已穿越（领先性）===")
    for key in ("ETH", "SOL"):
        win = data[key]
        btc_up = [w for w in common if data["BTC"][w]["high"] >= data["BTC"][w]["open"]*(1+x/100)]
        lead = sum(1 for w in btc_up if win[w]["high"] < win[w]["open"]*(1+x/100))
        print(f"  BTC 涨穿 {x}% 的 {len(btc_up)} 窗中，{key} 尚未涨穿: {lead} ({lead/len(btc_up)*100:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
