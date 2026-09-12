"""EV(ask) 分析：日志里的穿越信号 vs 窗口实际方向 → 每个价位买是否有 edge。

用与 momentum.window_outcome 相同的口径（窗口 K 线 close vs open）判定实际方向，
交叉日志里"窗口首个信号的方向 + 当时 ask"，得出：
  - 各 ask 价位上的信号真实胜率
  - EV = 胜率 − ask（买 1 股 ask，赢了得 1）
回答：是"上限太严"还是"策略无 edge"。
"""
from __future__ import annotations

import collections
import datetime
import re
import statistics
import sys

from pmbot.data_source import fetch_klines_batch

LOG = "logs/multi.log"
LINE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*rej sym=(\w+) win=(\d+) dir=(\w+) ask=([\d.]+)"
)


def first_signals(logs: list[str]) -> dict[tuple[str, int], tuple[str, float]]:
    """每个 (symbol, window) 的首次信号：方向与当时 ask（可跨多份日志）。"""
    out: dict[tuple[str, int], tuple[str, float]] = {}
    for path in logs:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = LINE_RE.match(line)
                if not m:
                    continue
                _ts, sym, win, d, ask = m.groups()
                out.setdefault((sym, int(win)), (d, float(ask)))
    return out


def outcomes(symbols: set[str], t0: int, t1: int) -> dict[tuple[str, int], str]:
    """各窗口实际方向：窗口 K 线 close vs open。"""
    res: dict[tuple[str, int], str] = {}
    for sym in sorted(symbols):
        try:
            ks = fetch_klines_batch(sym, "5m", since=t0 * 1000, limit=300, proxies=None)
        except Exception as e:
            print(f"  {sym}: K 线拉取失败 {type(e).__name__} {e}", file=sys.stderr)
            continue
        for k in ks:
            ts = int(k.timestamp) // 1000
            if not (t0 <= ts <= t1):
                continue
            o, c = float(k.open), float(k.close)
            if o > 0:
                res[(sym, ts)] = "up" if c > o else "down"
        print(f"  {sym}: {sum(1 for k in res if k[0] == sym)} 个窗口", file=sys.stderr)
    return res


def main() -> None:
    import glob

    logs = sorted(glob.glob("logs/multi.log*"))
    print(f"读取日志: {logs}")
    sig = first_signals(logs)
    print(f"日志里的窗口信号: {len(sig)}")
    wins = [w for _, w in sig]
    t0, t1 = min(wins), max(wins)
    print(f"窗口范围: {datetime.datetime.fromtimestamp(t0)} ~ "
          f"{datetime.datetime.fromtimestamp(t1)}")

    syms = {s for s, _ in sig}
    print("拉 K 线:", file=sys.stderr)
    out = outcomes(syms, t0, t1)

    pairs = [(d, a, out[(s, w)], w) for (s, w), (d, a) in sig.items() if (s, w) in out]
    print(f"可比对（信号+实际结果）: {len(pairs)}")
    if not pairs:
        return

    hit = sum(1 for d, _a, o, _w in pairs if d == o)
    print(f"\n总体：信号方向 = 实际方向 {hit}/{len(pairs)} = {hit/len(pairs)*100:.1f}%")

    days = collections.defaultdict(list)
    for d, a, o, w in pairs:
        days[datetime.datetime.fromtimestamp(w).strftime("%m-%d")].append((d, a, o))
    print("\n分日胜率（看是否单日行情特殊）:")
    for day in sorted(days):
        rows = days[day]
        h = sum(1 for d, _a, o in rows if d == o)
        ab = [a for _d, a, _o in rows]
        print(f"  {day}  n={len(rows):>4}  胜率={h/len(rows)*100:>5.1f}%  ask中位={statistics.median(ab):.3f}")

    print("\n按 ask 分档（买 ask、赢拿 1 → EV = 胜率 − ask）:")
    print(f"{'ask 档':>12} {'n':>5} {'胜率':>8} {'平均ask':>9} {'EV/股':>9}")
    buckets: dict[float, list[tuple[float, bool]]] = collections.defaultdict(list)
    for d, a, o, _w in pairs:
        buckets[round(a * 10) / 10].append((a, d == o))
    for b in sorted(buckets):
        rows = buckets[b]
        n = len(rows)
        wr = sum(1 for _a, w in rows if w) / n
        avg = statistics.mean(a for a, _w in rows)
        print(f"{b:.1f}-{b+0.1:.1f}".rjust(12) +
              f"{n:>5} {wr*100:>7.1f}% {avg:>9.3f} {wr-avg:>+9.3f}")

    print("\n只看实际成交过的区间（ask ≤ 0.60）:")
    sub = [(a, d == o) for d, a, o, _w in pairs if a <= 0.60]
    if sub:
        wr = sum(1 for _a, w in sub if w) / len(sub)
        print(f"  n={len(sub)} 胜率={wr*100:.1f}% 平均ask={statistics.mean(a for a,_ in sub):.3f}")


if __name__ == "__main__":
    main()
