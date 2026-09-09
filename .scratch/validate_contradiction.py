"""验证：反向矛盾过滤能否解释/避免昨晚实盘的亏损笔。

假设（来自 30 天 5m 数据）：标的穿越 ±X% 时，若 BTC 反向穿越，
标的结算方向命中率暴跌到 41%（vs 平均 80%）。

本脚本用昨晚 28 笔实盘交易验证：
  - 亏损笔是否集中在"BTC 反向"窗口
  - 若当时有反向过滤，能否避免亏损

用法：uv run python .scratch/validate_contradiction.py
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.data_source import fetch_klines_batch

STEP_5M = 300_000
X = 0.08


def main() -> int:
    rows = []
    for sym in ("SOL", "ETH"):
        p = Path(f"data_multi/{sym.lower()}/trades.csv")
        if not p.is_file():
            continue
        with open(p) as f:
            for r in csv.DictReader(f):
                r["symbol"] = sym
                r["entry_price"] = float(r["entry_price"])
                r["pnl"] = float(r["pnl"])
                rows.append(r)

    if not rows:
        print("无交易记录")
        return 1

    # 拉 BTC 5m K线覆盖交易窗口范围
    ws_min = min(int(r["window_start"]) for r in rows) * 1000 - STEP_5M
    ws_max = max(int(r["window_start"]) for r in rows) * 1000 + 2 * STEP_5M
    btc = {}
    cur = ws_min
    while cur < ws_max:
        try:
            batch = fetch_klines_batch("BTC", "5m", cur, 1000, proxies=None)
        except Exception as e:
            print(f"BTC 拉取失败: {e}", file=sys.stderr)
            break
        if not batch:
            break
        for k in batch:
            btc[k.timestamp] = k
        if len(batch) < 1000:
            break
        cur = batch[-1].timestamp + STEP_5M

    print(f"BTC K线 {len(btc)} 根，交易 {len(rows)} 笔\n")

    def btc_state(ws_sec: int, direction: str) -> str:
        ws = ws_sec * 1000
        k = btc.get(ws)
        if k is None:
            return "无BTC数据"
        o = k.open
        up = k.high >= o * (1 + X / 100)
        dn = k.low <= o * (1 - X / 100)
        if direction == "up":
            if dn:
                return "BTC反向跌"
            if up:
                return "BTC同向涨"
            return "BTC未动"
        else:
            if up:
                return "BTC反向涨"
            if dn:
                return "BTC同向跌"
            return "BTC未动"

    grp: dict[str, list] = {}
    for r in rows:
        st = btc_state(int(r["window_start"]), r["direction"])
        grp.setdefault(st, []).append(r)

    print(f"{'BTC状态':14s} {'笔数':>4s} {'胜率':>6s} {'PnL':>9s}  亏损笔")
    for st in ("BTC同向涨", "BTC同向跌", "BTC未动", "BTC反向涨", "BTC反向跌", "无BTC数据"):
        sub = grp.get(st, [])
        if not sub:
            continue
        w = sum(1 for r in sub if r["pnl"] > 0)
        pnl = sum(r["pnl"] for r in sub)
        losses = [f"{r['symbol']}{r['direction'][0].upper()}{r['entry_price']:.2f}" for r in sub if r["pnl"] < 0]
        print(f"{st:14s} {len(sub):4d} {w/len(sub)*100:5.0f}% {pnl:+8.2f}  {losses}")

    # 若过滤反向
    reverse = [r for r in rows if btc_state(int(r["window_start"]), r["direction"]).startswith("BTC反向")]
    kept = [r for r in rows if not btc_state(int(r["window_start"]), r["direction"]).startswith("BTC反向")]
    print(f"\n=== 若当时有反向过滤 ===")
    print(f"过滤掉 {len(reverse)} 笔（反向），其 pnl = {sum(r['pnl'] for r in reverse):+.2f}")
    w = sum(1 for r in kept if r["pnl"] > 0)
    print(f"保留 {len(kept)} 笔，胜率 {w/len(kept)*100:.0f}%，pnl = {sum(r['pnl'] for r in kept):+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
