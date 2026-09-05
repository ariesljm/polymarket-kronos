"""市场定价有效性审计：用真实 Polymarket 盘口录音重建「买价 → 结算期望」曲线。

数据源:
- data/recordings/calib_book_1s.csv —— record_join_ws 录制的 Polymarket 盘口 1s 序列
  (up_ask/up_bid/down_ask/down_bid + Binance 价)，填充率 ~92%
- gamma API —— 录音覆盖窗口的结算结果（Up wins?）

回答的问题（诺贝尔评审的落地点）:
A. 市场定价是否有效：买入价 p 分档的实际胜率 vs p（公平定价时胜率=p，期望=0）
B. 路径管理有无价值：带止盈/止损（现有 take_profit/stop_loss 参数）vs 持有到期
C. 哪些价带被系统高估/低估（Wilson 下限 > 0 才算显著边缘，且要盖过 3% 双边费）

用法: uv run python scripts/audit_market_pricing.py [--window N]
"""
from __future__ import annotations

import csv
import datetime
import json
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
STEP = 300  # 5m 窗口
FEE = 0.03  # 与 config taker_fee_pct 对齐
MAX_ENTRY_PRICE = 0.7          # 当前 config max_entry_price（审计同时看 0.7 上方的档）
TAKE_PROFIT = 0.7              # 百分比止盈（与 config 对齐）
STOP_LOSS = 0.6                # 百分比止损
TAKE_PROFIT_MAX = 0.95


def wilson_ci(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = correct / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (center - margin) / denom, (center + margin) / denom


def fetch_up_wins(ts: int) -> int | None:
    """gamma 拉窗口结算：Up 获胜=1，取不到=None。"""
    try:
        r = requests.get(f"{GAMMA}/events", params={"slug": f"btc-updown-5m-{ts}"},
                         headers=UA, timeout=20)
        events = r.json()
        m = events[0]["markets"][0]
        outs = json.loads(m["outcomes"])
        ops = json.loads(m["outcomePrices"])
        return int(ops[outs.index("Up")] == "1")
    except Exception:
        return None


def _f(s: str) -> float | None:
    try:
        return float(s) if s.strip() else None
    except ValueError:
        return None


def load_book(path: str) -> list[dict]:
    """读录音：(ts_ms, up_ask, up_bid, down_ask, down_bid)，跳过盘口缺失行。

    关键：record_join_ws 是读 bot 的 book.json 快照录盘——bot 停机时段
    book.json 不更新，录音时间戳照走但价格冻结在旧值。冻结价格与结算
    随机配对会伪造「低价低估」假象。返回行带 active 标记（该行价格相对
    前一行有变化=盘口活着），审计默认只用活跃行。
    """
    out = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not (r["up_ask"].strip() and r["down_ask"].strip()):
                continue
            out.append({
                "ts": int(r["ts"]) // 1000,
                "up_ask": _f(r["up_ask"]), "up_bid": _f(r["up_bid"]),
                "down_ask": _f(r["down_ask"]), "down_bid": _f(r["down_bid"]),
                "active": False,
            })
    for i in range(1, len(out)):
        if (out[i]["up_ask"], out[i]["down_ask"]) != (out[i - 1]["up_ask"], out[i - 1]["down_ask"]):
            out[i]["active"] = True
    return out


def entry_price_for_rows(rows: list[dict], window_start: int, side: str) -> float | None:
    """窗口开始后 60s 内第一个**盘口活着**（价格有变动）的 ask。

    冻结段价格是 bot 停机时 book.json 的陈旧值，与结算随机配对会伪造
    「低价低估」——只接受 active 行，否则返回 None（该窗口方向不可评估）。
    """
    lo = window_start
    for r in rows:
        if lo <= r["ts"] < lo + 60:
            if r.get("active") and r[f"{side}_ask"] is not None and r[f"{side}_ask"] > 0:
                return r[f"{side}_ask"]
        if r["ts"] >= lo + 60:
            break
    return None


def sim_exit(rows: list[dict], entry_ts: int, side: str, entry: float) -> dict:
    """持有到结算/止盈/止损三体模拟（1s 粒度，市价卖以 bid 成交）。

    take_profit = entry*1.7（封顶 0.95），stop_loss = entry*0.4，窗口结束结算 1/0。
    """
    tp = min(entry * (1 + TAKE_PROFIT), TAKE_PROFIT_MAX)
    sl = entry * (1 - STOP_LOSS)
    position = None  # 1=分仓投标的持仓状态
    for r in rows:
        if r["ts"] < entry_ts:
            continue
        if r["ts"] >= entry_ts + STEP:
            break  # 窗口结束 → 结算（下方处理）
        bid = r[f"{side}_bid"]
        if bid is None:
            continue
        if bid >= tp:
            return {"exit": bid, "reason": "take_profit", "exit_ts": r["ts"]}
        if bid <= sl:
            return {"exit": bid, "reason": "stop_loss", "exit_ts": r["ts"]}
    return {"exit": None, "reason": "settle", "exit_ts": entry_ts + STEP}


def pnl_of(entry: float, exit_price: float | None, up_wins: int, side: str) -> float:
    """份额 = 1/ask；买 up 结算赢 → 1.0；买 down 结算赢（up_wins=0）→ 1.0。"""
    size = 1.0 / entry
    if exit_price is None:  # 结算
        won = (up_wins == 1) if side == "up" else (up_wins == 0)
        exit_price = 1.0 if won else 0.0
    cost = size * entry * (1 + FEE)
    proceeds = size * exit_price * (1 - FEE)
    return proceeds - cost


def main() -> None:
    import os
    side_filter = sys.argv[1] if len(sys.argv) > 1 else "all"  # all / up / down
    path = os.path.join("data", "recordings", "calib_book_1s.csv")
    rows = load_book(path)
    print(f"盘口录音: {len(rows)} 行（1s 粒度，{datetime.datetime.fromtimestamp(rows[0]['ts'], datetime.timezone.utc)}"
          f" ~ {datetime.datetime.fromtimestamp(rows[-1]['ts'], datetime.timezone.utc)}）")
    if not rows:
        sys.exit("无数据")

    # ---- 窗口集合与结算拉取 ----
    lo_win = rows[0]["ts"] // STEP * STEP
    hi_win = rows[-1]["ts"] // STEP * STEP
    windows = list(range(lo_win, hi_win + STEP, STEP))
    print(f"窗口数: {len(windows)}，拉取 gamma 结算…")
    with ThreadPoolExecutor(max_workers=8) as ex:
        wins_map = dict(zip(windows, ex.map(fetch_up_wins, windows)))
    ok = sum(1 for v in wins_map.values() if v is not None)
    print(f"结算可取: {ok}/{len(windows)}")
    if ok:
        up_win_rate = sum(1 for v in wins_map.values() if v == 1) / ok
        print(f"录音时段 Up 胜率: {up_win_rate:.1%}（<50% = BTC 下跌期，Down 低价低估可能是时段效应）")

    # ---- 每个窗口两个方向的模拟 ----
    candidates = []  # (window, side, entry, pnl, exit_reason)
    by_side = {s: 0 for s in ("up", "down")}
    for w in windows:
        if wins_map.get(w) is None:
            continue
        win = wins_map[w]
        win_rows = [r for r in rows if w <= r["ts"] < w + STEP and r.get("active")]
        for side in ("up", "down"):
            if side_filter not in ("all", side):
                continue
            entry = entry_price_for_rows(win_rows, w, side)
            if entry is None or not (0 < entry < 1):
                continue
            res = sim_exit(win_rows, w, side, entry)
            pnl = pnl_of(entry, res["exit"], win, side)
            candidates.append((w, side, entry, pnl, res["reason"]))
            by_side[side] += 1

    cand = candidates
    print(f"可评估候选: {len(cand)}（up {by_side['up']} / down {by_side['down']}，方向过滤={side_filter}）")
    if len(cand) < 50:
        sys.exit("样本过少")

    # ---- 分析 A：市场定价有效性（持有到结算的期望 vs 入场价）----
    # 对每个候选窗口只统计「市场公平性」：结算期望 = P(win|价格) − 价格。
    # 用 up/down 合并：定义公平检验 = 结算赢率 − 买入价（=1/ask 隐含概率）。
    print("\n" + "=" * 78)
    print("A. 市场定价有效性：买入价 p 分档 → 实际结算胜率（公平市场时应等于 p）")
    print("=" * 78)
    print(f"{'买入价档':>10} {'n':>4} {'实际胜率':>8} {'价中点':>6} {'偏离':>7} {'Wilson下限':>9} {'结论':>8}")
    buckets = {}
    for w, side, entry, pnl, reason in cand:
        b = math.floor(entry * 10) / 10
        buckets.setdefault(b, []).append((side, entry, pnl, reason))
    for b in sorted(buckets):
        sub = buckets[b]
        n = len(sub)
        if n < 5:
            continue
        # 市场有效性口径：只统计持有到结算的候选（路径管理影响留在 B 分析）
        mid = statistics.fmean(e for _, e, _, _ in sub)
        settle_cands = [c for c in sub if c[3] == "settle"]
        sc = len(settle_cands)
        if sc:
            sw = sum(1 for s, e, p, _ in settle_cands if p > 0)  # pnl>0 ⇔ 结算方向正确
            lb, _ = wilson_ci(sw, sc)
            dev = sw / sc - mid
            verdict = "低估→买" if lb > mid + 0.02 else ("高估→买" if (1 - lb) < mid - 0.02 else "持平")
            print(f"{b:>8.1f}-{(b+0.1):<4.1f} {sc:>4} {sw/sc:>7.1%} {mid:>6.3f} {dev:>+7.1%} {lb:>8.1%} {verdict:>8}")

    # ---- 分析 B：路径管理价值（带止盈/止损 vs 持有到期）----
    print("\n" + "=" * 78)
    print("B. 路径管理：买入价 p 分档的期望收益（3% 双边费，止盈/止损/结算三体模拟）")
    print("=" * 78)
    print(f"{'买入价档':>10} {'n':>4} {'期望/笔':>8} {'胜率':>6} {'止盈':>5} {'止损':>5} {'结算':>5} {'显著性(t)':>8}")
    for b in sorted(buckets):
        sub = buckets[b]
        n = len(sub)
        if n < 5:
            continue
        pnls = [c[2] for c in sub]
        mean = statistics.fmean(pnls)
        sd = statistics.stdev(pnls) if n > 1 else 0.0
        t = mean / (sd / math.sqrt(n)) if sd else 0.0
        wr = sum(1 for p in pnls if p > 0) / n
        reasons = [c[3] for c in sub]
        r_tp = reasons.count("take_profit") / n
        r_sl = reasons.count("stop_loss") / n
        r_st = reasons.count("settle") / n
        sign = "*" if abs(t) >= 1.96 else " "
        print(f"{b:>8.1f}-{(b+0.1):<4.1f} {n:>4} {mean:>+8.4f} {wr:>5.1%} {r_tp:>5.1%} {r_sl:>5.1%} {r_st:>5.1%} {sign}{t:>7.2f}")

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("C. 汇总")
    print("=" * 78)
    pnls = [c[2] for c in cand]
    mean = statistics.fmean(pnls)
    sd = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    t = mean / (sd / math.sqrt(len(pnls))) if sd else 0.0
    med = statistics.median(pnls)
    # 极端单会把均值/总显著性拉爆（历史 5.19 大单、0.0-0.1 档同类陷阱）：
    # 同时给中位数与 win/lose 均值，均值只作参考
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    print(f"全部候选: 均值 {mean:+.4f}/笔（t={t:.2f}，n={len(pnls)}）| 中位数 {med:+.4f} | "
          f"赢均值 {statistics.fmean(wins) if wins else 0:+.4f} / 输均值 {statistics.fmean(losses) if losses else 0:+.4f} | "
          f"命中 {len(wins)}/{len(pnls)} ({len(wins)/len(pnls):.0%})")
    if abs(t) >= 1.96:
        print("  ⚠️ 均值显著需复核极端单: 看中位数与命中率是否同向；仅均值一侧显著多为单笔驱动")
    for b in sorted(buckets):
        sub = buckets[b]
        if len(sub) >= 5:
            mid = statistics.fmean(e for _, e, _, _ in sub)
            pn = [c[2] for c in sub]
            m = statistics.fmean(pn)
            if m > 0:
                print(f"  正期望档 {b:.1f}: {len(sub)} 笔，均值 {m:+.4f}（价中点 {mid:.3f}）")


if __name__ == "__main__":
    main()