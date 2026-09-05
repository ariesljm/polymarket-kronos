"""结束前「确定赢方向」回测：窗口结束前 X 秒,买盘口已基本确定的方向。

用户观察:Polymarket 网页端在距结束 ~120s 出现大量成交单——其他 bot 在
吃「方向已确定但价格还没到 1」的残差利润。本脚本用录音数据模拟验证:
- 判定确定赢:某方向 bid ≥ threshold(如 0.85)且为双方向较高者
- 入场价 = 该方向 ask(实际成交价),持有到结算(赢→1.0,输→0)
- 3% 双边手续费;只统计盘口活跃段(冻结数据已被审计证伪,不可用)

用法: uv run python scripts/sim_settled_entries.py
"""
from __future__ import annotations

import csv
import datetime
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
STEP = 300
FEE = 0.03


def _f(s: str) -> float | None:
    try:
        return float(s) if s.strip() else None
    except ValueError:
        return None


def load_book(path: str) -> list[dict]:
    """读录音并标记活跃行(价格相对前行有变化=盘口活着;冻结行不可用于结算分析)。"""
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


def fetch_up_wins(ts: int) -> int | None:
    try:
        r = requests.get(f"{GAMMA}/events", params={"slug": f"btc-updown-5m-{ts}"},
                         headers=UA, timeout=20)
        m = r.json()[0]["markets"][0]
        outs = __import__("json").loads(m["outcomes"])
        ops = __import__("json").loads(m["outcomePrices"])
        return int(ops[outs.index("Up")] == "1")
    except Exception:
        return None


def quote_at(rows: list[dict], ts: int, tol: int = 30) -> dict | None:
    """ts 附近最近一条活跃行(±tol 秒),无活跃行返回 None。"""
    best = None
    for r in rows:
        if not r.get("active"):
            continue
        if abs(r["ts"] - ts) <= tol:
            if best is None or abs(r["ts"] - ts) < abs(best["ts"] - ts):
                best = r
        elif r["ts"] > ts + tol:
            break
    return best


def main() -> None:
    rows = load_book("data/recordings/calib_book_1s.csv")
    if not rows:
        sys.exit("无数据")
    lo = rows[0]["ts"] // STEP * STEP
    hi = rows[-1]["ts"] // STEP * STEP
    windows = list(range(lo, hi + STEP, STEP))
    with ThreadPoolExecutor(max_workers=8) as ex:
        wins_map = dict(zip(windows, ex.map(fetch_up_wins, windows)))

    print("结束前 X 秒入场回测(3% 双边费,仅盘口活跃段)")
    print(f"{'X秒':>4} {'阈值':>5} {'候选':>5} {'命中':>6} {'期望/笔':>8} {'中位':>8} {'t':>6} {'赢均值':>7} {'输均值':>7}")
    for x in (120, 90, 60, 30):
        for thr in (0.80, 0.85, 0.90, 0.95):
            pnls, wins = [], 0
            for w in windows:
                if wins_map.get(w) is None:
                    continue
                q = quote_at(rows, w + STEP - x)
                if q is None:
                    continue
                up_certain = q["up_bid"] is not None and q["up_bid"] >= thr and q["up_bid"] >= (q["down_bid"] or 0)
                down_certain = q["down_bid"] is not None and q["down_bid"] >= thr and q["down_bid"] >= (q["up_bid"] or 0)
                if up_certain == down_certain:  # 都不确定或都声称确定(取更高者处理)
                    if up_certain and down_certain:
                        up_certain = (q["up_bid"] or 0) >= (q["down_bid"] or 0)
                        down_certain = not up_certain
                    else:
                        continue
                entry = (q["up_ask"] if up_certain else q["down_ask"])
                if entry is None:
                    continue
                size = 1.0 / entry
                won = (wins_map[w] == 1) if up_certain else (wins_map[w] == 0)
                cost = size * entry * (1 + FEE)
                proceeds = size * 1.0 * (1 - FEE) if won else 0.0
                pnls.append(proceeds - cost)
                wins += won
            if not pnls:
                continue
            n = len(pnls)
            mean = statistics.fmean(pnls)
            sd = statistics.stdev(pnls) if n > 1 else 0.0
            t = mean / (sd / math.sqrt(n)) if sd else 0.0
            winc = [p for p in pnls if p > 0]
            losec = [p for p in pnls if p <= 0]
            sign = "*" if abs(t) >= 1.96 else " "
            print(f"{x:>4} {thr:>5.2f} {n:>5} {wins/n:>5.1%} {mean:>+8.4f} {statistics.median(pnls):>+8.4f} {sign}{t:>5.2f} "
                  f"{statistics.fmean(winc) if winc else 0:>+7.4f} {statistics.fmean(losec) if losec else 0:>+7.4f}")


if __name__ == "__main__":
    main()