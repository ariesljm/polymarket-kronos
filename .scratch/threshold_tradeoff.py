"""分标的阈值-命中率权衡扫描（5 标的，30 天 5m K线）。

背景：现有阈值（ETH 0.10 / SOL 0.12 / XRP 0.15 / DOGE 0.15 / BNB 0.10）是按**命中率**
最优挑的，但实盘证明高阈值 = 信号更晚 = 盘口已跳变到 0.63+ = 0 成交。
全部 35 笔成交都发生在全局阈值 0.08 时期。

本表给出权衡的两端，供选阈值：
  - 命中率 q：穿越方向 == 结算方向 的比例（阈值越高越好）
  - 触发数：30 天内有多少窗口触发（阈值越低越多）→ 成交机会的上限

口径与项目校准同源：窗口内 high/low 触及阈值即触发；close vs open 定胜负。

用法: uv run python .scratch/threshold_tradeoff.py [--days 30]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.data_source import fetch_klines_batch

STEP_MS = 300_000
PAGE = 1000
SYMS = ("ETH", "SOL", "XRP", "DOGE", "BNB")
THRESHOLDS = (0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20)


def fetch_n(symbol: str, days: int) -> dict[int, dict]:
    now_ms = int(time.time() * 1000)
    cur, out = now_ms - days * 86400_000, {}
    while cur < now_ms:
        try:
            batch = fetch_klines_batch(symbol, "5m", cur, PAGE, proxies=None)
        except Exception as e:
            print(f"  {symbol} 拉取失败: {e}", file=sys.stderr)
            break
        if not batch:
            break
        for k in batch:
            out[k.timestamp - (k.timestamp % STEP_MS)] = {
                "open": k.open, "close": k.close, "high": k.high, "low": k.low,
            }
        if len(batch) < PAGE:
            break
        cur = batch[-1].timestamp + STEP_MS
        time.sleep(0.1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    print(f"=== 拉取 5m K线 {args.days} 天 × {len(SYMS)} 标的 ===")
    data = {}
    for s in SYMS:
        data[s] = fetch_n(s, args.days)
        print(f"  {s}: {len(data[s])} 窗")

    print(f"\n=== 阈值 ↔ 命中率 / 触发窗口数（{args.days} 天）===")
    print(f"{'阈值':>6} | " + " | ".join(f"{s:^15}" for s in SYMS))
    print(f"{'':>6} | " + " | ".join(f"{'触发':>6} {'命中':>7}" for _ in SYMS))
    for x in THRESHOLDS:
        cells = []
        for s in SYMS:
            d = data[s]
            trig = hits = 0
            for w in d.values():
                o = w["open"]
                if w["high"] >= o * (1 + x / 100):
                    trig += 1
                    hits += w["close"] > o
                if w["low"] <= o * (1 - x / 100):
                    trig += 1
                    hits += w["close"] < o
            cells.append(f"{trig:6d} {hits/trig*100:6.1f}%" if trig else f"{0:6d} {'--':>7}")
        print(f"{x:6.2f} | " + " | ".join(cells))

    print("\n注: '触发' 含上下两向（一窗可能双向各计一次）；命中率按触发次数加权。")
    print("    触发数 ÷ 2 约为'有信号的窗口数'上限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
