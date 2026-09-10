"""EV(ask) 分析：把拒单日志变成"每个 ask 档还剩多少 edge"的曲线。

背景（2026-09-10 实测）：动量信号 113 次全部被 entry_price_cap 拦下
（1693 次拒单的 ask 最低 0.63，cap=0.50），成交枯竭（20→8→7→0 笔/天）。
要回答两个问题，都靠这张表而不是拍脑袋调参：

  1. 真实入场上限该在哪 —— 哪个 ask 档 EV = q − p 转负，那里就是 edge 边界
  2. 阈值该下调多少才有正 EV —— 配合"信号越早、ask 越低"的关系反推

方法：
  1. 解析日志里 execution_dispatcher 追加的结构化后缀：
     `| rej sym=ETH win=1789000000 dir=down ask=0.950 reason=cap`
  2. 每个 (sym, win, dir) 只留 ask 最低的一条（窗口内会重复触发；同一窗口
     可能上下都穿越 → 两个机会各自成行，脚本会报告此类窗口数）
  3. 拉 Binance 5m K线，按项目校准同源口径判胜负：
     dir=up  命中 ⟺ close > open
     dir=down 命中 ⟺ close < open      （平盘两者都不算命中）
  4. 按 ask 分档汇总 q（命中率）与 EV = q − p（p = 该档实际均价）

用法:
  uv run python .scratch/ev_by_ask.py                # 默认读 logs/multi*.log
  uv run python .scratch/ev_by_ask.py --bucket 0.1
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmbot.data_source import fetch_klines_batch

REJ_RE = re.compile(
    r"\| rej sym=(?P<sym>\S+) win=(?P<win>\d+) dir=(?P<dir>\w+) ask=(?P<ask>\S+) reason=(?P<reason>\w+)"
)
STEP_MS = 300_000
PAGE = 1000


def parse_logs(patterns: list[str]) -> tuple[dict, int]:
    """→ ({(sym, win, dir): (ask, reason)}, 解析到的总行数)。"""
    best: dict[tuple, tuple[float, str]] = {}
    total = 0
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = REJ_RE.search(line)
                    if not m or m["ask"] == "none":
                        continue
                    total += 1
                    try:
                        ask = float(m["ask"])
                    except ValueError:
                        continue
                    key = (m["sym"], int(m["win"]), m["dir"])
                    if key not in best or ask < best[key][0]:
                        best[key] = (ask, m["reason"])
    return best, total


def fetch_windows(symbol: str, lo_ms: int, hi_ms: int) -> dict[int, tuple[float, float]]:
    """→ {window_open_ms: (open, close)}，翻页直到覆盖 hi_ms。"""
    out: dict[int, tuple[float, float]] = {}
    cur = lo_ms
    while cur <= hi_ms:
        try:
            batch = fetch_klines_batch(symbol, "5m", cur, PAGE, proxies=None)
        except Exception as e:  # 网络失败不该中断整个分析
            print(f"  {symbol} 拉取失败({e})，已获取 {len(out)} 窗", file=sys.stderr)
            break
        if not batch:
            break
        for k in batch:
            out[k.timestamp] = (k.open, k.close)
        if len(batch) < PAGE:
            break
        cur = batch[-1].timestamp + STEP_MS
        time.sleep(0.15)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", action="append", default=None,
                    help="日志 glob（可多次指定）；默认 logs/multi.log + 轮转文件")
    ap.add_argument("--bucket", type=float, default=0.05, help="ask 分档宽度（默认 0.05）")
    args = ap.parse_args()
    patterns = args.log or ["logs/multi.log", "logs/multi.log.*"]

    best, total = parse_logs(patterns)
    if not best:
        print("没有解析到拒单记录（`| rej ...`）。确认 bot 已用新代码跑过一段时间。")
        return 1
    print(f"解析到拒单行 {total} 条 → 去重后 (标的,窗口,方向) 机会 {len(best)} 个")

    # 同一窗口出现上下两个方向的机会数（一次只能持一仓，属乐观偏差）
    by_win: dict[tuple, set] = defaultdict(set)
    for sym, win, d in best:
        by_win[(sym, win)].add(d)
    multi = sum(1 for v in by_win.values() if len(v) > 1)
    if multi:
        print(f"  提示: 其中 {multi} 个窗口上下都触发（上限乐观偏差，占比 {multi/len(by_win)*100:.0f}%）")

    # 拉 K 线判胜负
    print("\n=== 拉 Binance 5m K线 ===")
    need: dict[str, list[int]] = defaultdict(list)
    for sym, win, _ in best:
        need[sym].append(win)
    wins: dict[str, dict[int, tuple[float, float]]] = {}
    for sym, wl in need.items():
        lo, hi = min(wl) * 1000, max(wl) * 1000
        wins[sym] = fetch_windows(sym, lo, hi)
        print(f"  {sym}: 需 {len(set(wl))} 窗，取到 {len(wins[sym])} 窗")

    # 汇总
    rows = []  # (ask, hit)
    missing = 0
    for (sym, win, direction), (ask, reason) in best.items():
        k = wins.get(sym, {}).get(win * 1000)
        if k is None:
            missing += 1
            continue
        o, c = k
        hit = (c > o) if direction == "up" else (c < o)
        rows.append((ask, bool(hit)))
    if missing:
        print(f"  跳过 {missing} 个缺 K 线的窗口")
    if not rows:
        print("没有可对齐的机会，退出。")
        return 1

    n_all = len(rows)
    q_all = sum(h for _, h in rows) / n_all
    p_all = sum(a for a, _ in rows) / n_all
    print(f"\n=== 总体（所有被拒机会，n={n_all}）===")
    print(f"  命中率 q = {q_all*100:.1f}%   均价 p = {p_all:.3f}   EV/笔 = {q_all - p_all:+.3f}")

    # 分档
    print(f"\n=== 按 ask 分档（档宽 {args.bucket}）===")
    print(f"  {'ask 档':>12} | {'n':>4} | {'q 命中率':>8} | {'均价':>6} | {'EV/笔':>7}")
    buckets: dict[int, list] = defaultdict(list)
    for ask, hit in rows:
        buckets[int(ask / args.bucket)].append((ask, hit))
    for idx in sorted(buckets):
        lo = idx * args.bucket
        v = buckets[idx]
        q = sum(h for _, h in v) / len(v)
        p = sum(a for a, _ in v) / len(v)
        print(f"  {lo:.2f}-{lo+args.bucket:.2f} | {len(v):4d} | {q*100:7.1f}% | {p:6.3f} | {q-p:+7.3f}")

    # 累计（给定上限下会成交多少、EV 多少）—— 直接读作"cap 该设在哪"
    print("\n=== 累计视角：cap 设在该值以下会怎样 ===")
    print(f"  {'cap':>5} | {'可成交 n':>8} | {'q':>6} | {'均价':>6} | {'EV/笔':>7} | {'EV 合计':>8}")
    for cap in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.90, 1.00]:
        v = [(a, h) for a, h in rows if a <= cap]
        if not v:
            print(f"  {cap:5.2f} | {0:8d} | {'--':>6} | {'--':>6} | {'--':>7} | {'--':>8}")
            continue
        q = sum(h for _, h in v) / len(v)
        p = sum(a for a, _ in v) / len(v)
        print(f"  {cap:5.2f} | {len(v):8d} | {q*100:5.1f}% | {p:6.3f} | {q-p:+7.3f} | {(q-p)*len(v):+8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
