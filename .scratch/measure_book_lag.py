"""验证：Binance 快速大动后，Polymarket 盘口的滞后秒数（latency 套利核心机制）。

数据：data/recordings/calib_book_1s.csv（BTC 3 天秒级：binance_price + up/down ask/bid）。

方法：
  1. 计算 binance_price 滚动 60s 变化率
  2. 找 |变化率| >= 0.3% 的"快速大动"事件
  3. 测量事件后 up_ask 多久才反映（从事件前基线涨到对应水平）

若滞后 30-90 秒 → latency 套利成立（chudi.dev 对）；
若滞后 <1-2 秒 → 盘口几乎即时反映（学术 347ms 对），latency 套利空间极窄。

用法：uv run python .scratch/measure_book_lag.py
"""
from __future__ import annotations

import csv
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    path = Path("data/recordings/calib_book_1s.csv")
    if not path.is_file():
        print("找不到 calib_book_1s.csv")
        return 1

    # 载入有效行（binance_price 非空）
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            p = r.get("binance_price")
            if not p:
                continue
            try:
                rows.append((int(r["ts"]), float(p),
                             float(r["up_ask"]) if r.get("up_ask") else None))
            except (ValueError, TypeError):
                continue
    print(f"有效秒级样本 {len(rows)} 行")

    # 滚动 60s 窗口检测快速大动
    THRESH = 0.003  # 0.3%
    WINDOW_MS = 60_000
    events = []
    win = deque()
    for ts, price, _ in rows:
        win.append((ts, price))
        while win and ts - win[0][0] > WINDOW_MS:
            win.popleft()
        if len(win) < 2:
            continue
        base = win[0][1]
        move = (price - base) / base
        if abs(move) >= THRESH:
            events.append((ts, move, base, price))
            # 冷却：一个大动事件持续期内只记一次
            win.clear()
    print(f"快速大动事件（60s |Δ|>=0.3%）: {len(events)} 次")

    # 对每个事件，测量 up_ask 从事件前基线到"反映"的延迟
    # 简化：找事件时刻前后 up_ask 的变化
    lags = []
    price_ts = {ts: (price, up_ask) for ts, price, up_ask in rows}
    for ev_ts, move, base, price in events[:200]:
        direction = 1 if move > 0 else -1
        # 事件前 10s 的 up_ask 基线
        pre = [v[1] for t, v in price_ts.items() if ev_ts - 10_000 <= t < ev_ts and v[1] is not None]
        if not pre:
            continue
        baseline = sum(pre) / len(pre)
        # 事件后 up_ask 何时偏离基线 > 0.05（反映）
        reflected_at = None
        for t in range(ev_ts, ev_ts + 120_000, 1000):
            v = price_ts.get(t)
            if v is None or v[1] is None:
                continue
            ask = v[1]
            if direction > 0 and ask >= baseline + 0.05:
                reflected_at = t
                break
            if direction < 0 and ask <= baseline - 0.05:
                reflected_at = t
                break
        if reflected_at is not None:
            lags.append((reflected_at - ev_ts) / 1000)
        else:
            lags.append(float("inf"))

    if lags:
        finite = [l for l in lags if l != float("inf")]
        print(f"\n=== up_ask 反映延迟（事件后偏离基线 >0.05 的秒数）===")
        print(f"  样本 {len(lags)}，其中 {len(finite)} 个在 120s 内反映")
        if finite:
            import statistics
            print(f"  中位延迟: {statistics.median(finite):.1f}s")
            print(f"  平均延迟: {statistics.mean(finite):.1f}s")
            under5 = sum(1 for l in finite if l <= 5)
            under30 = sum(1 for l in finite if l <= 30)
            print(f"  <=5s: {under5}/{len(finite)} = {under5/len(finite)*100:.0f}%")
            print(f"  <=30s: {under30}/{len(finite)} = {under30/len(finite)*100:.0f}%")
        print(f"  未在 120s 内反映: {len(lags)-len(finite)} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
