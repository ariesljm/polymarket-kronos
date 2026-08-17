"""交易模拟回测：限价等回调 vs 市价入场 + 百分比止盈止损。

数据：
- 模型输入：Binance 5m K 线（与在线一致）
- 价格路径：Polymarket Data API 逐笔成交（data-api.polymarket.com/trades）
  按 yes token 过滤、按时间排序、每秒均价降采样

用法: uv run python -m pmbot.backtest_sim [--points 30] [--samples 10] [--amount 1.0]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import pandas as pd
import requests

from pmbot.backtest import fetch_klines
from pmbot.exit_rules import position_exit_levels
from pmbot.constants import window_start_sec
from pmbot.predictor import KronosPredictorClient

GAMMA = "https://gamma-api.polymarket.com/markets"
DATA_API = "https://data-api.polymarket.com/trades"
# 代理优先环境变量（与 run.py 一致），无则回退本机默认代理
PROXIES = {"https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10808")}

LIMIT_PRICE = 0.5      # 限价模式挂单价
TP_PCT = 0.30          # 百分比止盈（相对入场价）
SL_PCT = 0.20          # 百分比止损
TAKER_FEE_RATE = 0.07  # crypto taker 费率（官方公式参数）
MIN_ORDER_SIZE = 5     # 限价单最小股数（/book min_order_size）


def fetch_window(cid: str, tokens: list[str], win_start: int, win_end: int) -> list[tuple[int, float]]:
    """拉取窗口内逐笔成交（分页），返回按时间排序的 [(ts, price)]（yes token，秒级均价）。"""
    yes_id = tokens[0]
    rows: list[dict] = []
    offset = 0
    while True:
        r = requests.get(DATA_API, params={"market": cid, "limit": 1000, "offset": offset},
                         proxies=PROXIES, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        # 已覆盖到窗口开始之前
        if min(x["timestamp"] for x in batch) < win_start:
            break
        offset += len(batch)
        time.sleep(0.1)
    # 过滤 yes token + 窗口内时间，按秒聚合均价
    per_sec: dict[int, list[float]] = {}
    for x in rows:
        if x.get("asset") != yes_id:
            continue
        ts = x.get("timestamp")
        p = x.get("price")
        if ts is None or p is None or not (win_start <= ts <= win_end):
            continue
        per_sec.setdefault(ts // 1, []).append(float(p))
    path = sorted((ts, sum(ps) / len(ps)) for ts, ps in per_sec.items())
    return path


def simulate_path(path: list[tuple[int, float]], entry_price: float, size: float,
                  is_taker: bool, tp_pct: float = TP_PCT, sl_pct: float = SL_PCT) -> dict:
    """沿成交路径模拟持仓管理。path: [(ts, price), ...] 升序。

    tp_pct/sl_pct 默认模块内置值；run() 从 config 注入实盘参数
    （与 exit_rules 单一事实源精神闭环：公式与参数同源）。
    """
    tp, sl = position_exit_levels(entry_price, tp_pct, sl_pct)  # 共享公式源（tp_max=0.999, floor=0.001 默认）
    t0 = path[0][0]
    for i, (ts, price) in enumerate(path):
        if price >= tp:
            return _trade(entry_price, tp, size, is_taker, "take_profit", ts - t0)
        if price <= sl:
            return _trade(entry_price, sl, size, is_taker, "stop_loss", ts - t0)
    return _trade(entry_price, path[-1][1], size, is_taker, "window_end", path[-1][0] - t0)


def _trade(entry: float, exit_p: float, size: float, is_taker: bool, reason: str, hold_s: float) -> dict:
    fee = size * TAKER_FEE_RATE * entry * (1 - entry) if is_taker else 0.0
    return {"entry": entry, "exit": exit_p, "size": size, "fee": fee,
            "pnl": (exit_p - entry) * size - fee, "reason": reason, "hold_s": hold_s}


def fetch_market(win_start: int) -> dict | None:
    """gamma 查历史窗口市场：condition_id + [yes, no] token。"""
    slug = f"btc-updown-5m-{win_start}"
    r = requests.get(GAMMA, params={"slug": slug}, proxies=PROXIES, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    m = rows[0]
    return {"cid": m["conditionId"], "tokens": json.loads(m["clobTokenIds"])}


def trade_params(args: argparse.Namespace) -> tuple[float, float, float, float]:
    """止盈/止损/入场阈值参数：CLI 覆盖 > config.yaml > 内置默认。

    与 exit_rules 单一事实源精神闭环：回测跟随实盘配置（config 调整
    止盈止损/入场阈值后离线模拟口径一致），CLI 可显式覆盖研究设定。
    入场阈值曾硬编码 0.55/0.5（engine 用 config.p_up_buy → 同一规则第三份实现）。
    """
    tp, sl, pub, pdb = args.tp, args.sl, args.p_up_buy, args.p_down_buy
    if tp is None or sl is None or pub is None or pdb is None:
        try:
            from pmbot.config import load_config

            cfg = load_config(args.config)
            tp = tp if tp is not None else cfg.take_profit
            sl = sl if sl is not None else cfg.stop_loss
            pub = pub if pub is not None else cfg.p_up_buy
            pdb = pdb if pdb is not None else (1 - cfg.p_up_buy)
        except Exception:
            tp = tp if tp is not None else TP_PCT
            sl = sl if sl is not None else SL_PCT
            pub = pub if pub is not None else 0.55
            pdb = pdb if pdb is not None else 0.50
    return tp, sl, pub, pdb


def run(args: argparse.Namespace) -> int:
    ctx = 512  # kronos-small 上下文
    need5m = ctx + args.points + 1
    print(f"拉取 5m×{need5m} 根（{args.symbol} / {args.variant}）...")
    df5 = fetch_klines(args.symbol, "5m", need5m)
    df5 = df5.sort_values("timestamp").reset_index(drop=True)

    tp_pct, sl_pct, p_up_buy, p_down_buy = trade_params(args)
    client = KronosPredictorClient(variant=args.variant)
    t0 = time.time()
    limit_trades, market_trades = [], []

    for k in range(len(df5) - args.points, len(df5)):
        win_start = int(df5["timestamp"].iloc[k])
        baseline = float(df5["close"].iloc[k - 1])
        m = fetch_market(win_start)
        if m is None:
            continue
        path = fetch_window(m["cid"], m["tokens"], win_start, win_start + 300)
        if len(path) < 3:
            continue

        # ---- 模型预测（窗口开始时，用已闭合 5m K 线）----
        x = df5.iloc[k - ctx:k].reset_index(drop=True)
        preds = client.predict_targets(x, args.samples)
        p_up = sum(1 for v in preds if v > baseline) / len(preds)
        if p_down_buy <= p_up <= p_up_buy:
            continue  # 未达入场阈值（config.p_up_buy / 1−p_up_buy）
        direction = "UP" if p_up > p_up_buy else "DOWN"

        # ---- 市价模式：推理完成即按路径首笔成交价入场 ----
        entry_m = path[0][1]
        size_m = args.amount / entry_m  # 按金额，份额可小数
        tr_m = simulate_path(path[1:], entry_m, size_m, is_taker=True,
                             tp_pct=tp_pct, sl_pct=sl_pct)
        market_trades.append(tr_m)

        # ---- 限价模式：路径上成交价 ≤ LIMIT_PRICE 才入场 ----
        tr_l = None
        for i, (_, price) in enumerate(path):
            if price <= LIMIT_PRICE:
                entry_l = price
                size_l = max(MIN_ORDER_SIZE, int(1 / entry_l) + 1)
                tr_l = simulate_path(path[i + 1:], entry_l, size_l, is_taker=True,
                                     tp_pct=tp_pct, sl_pct=sl_pct)
                break
        if tr_l is not None:
            limit_trades.append(tr_l)

    def stats(trades: list[dict]) -> dict | None:
        if not trades:
            return None
        pnl = sum(t["pnl"] for t in trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        fees = sum(t["fee"] for t in trades)
        return {"n": len(trades), "win": wins / len(trades), "pnl": pnl,
                "fee": fees, "avg": pnl / len(trades),
                "tp": sum(1 for t in trades if t["reason"] == "take_profit"),
                "sl": sum(1 for t in trades if t["reason"] == "stop_loss")}

    s_l, s_m = stats(limit_trades), stats(market_trades)
    print(f"\n评估 {args.points} 个窗口（耗时 {time.time() - t0:.0f}s，{args.samples} 采样/窗口，"
          f"金额 ${args.amount}，TP {tp_pct:.0%} / SL {sl_pct:.0%}，真实成交路径）")
    print("=" * 68)
    for name, s in (("限价(等回调≤0.50)", s_l), ("市价(立即入场)", s_m)):
        if s is None:
            print(f"{name}: 无成交"); continue
        print(f"{name}: {s['n']} 笔  胜率 {s['win']:.1%}  总盈亏 {s['pnl']:+.3f} USDC"
              f"  均 {s['avg']:+.4f}  费 {s['fee']:.3f}")
        print(f"  止盈 {s['tp']} / 止损 {s['sl']}")
    print("=" * 68)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="限价 vs 市价交易模拟回测（真实成交路径）")
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--points", type=int, default=30, help="评估窗口数")
    parser.add_argument("--samples", type=int, default=10, help="每窗口采样次数")
    parser.add_argument("--amount", type=float, default=1.0, help="市价模式每注金额（USDC）")
    parser.add_argument("--variant", default="kronos-small")
    parser.add_argument("--config", default="config.yaml", help="配置文件（TP/SL/入场阈值参数源）")
    parser.add_argument("--tp", type=float, default=None, help="止盈百分比（覆盖 config，如 0.30）")
    parser.add_argument("--sl", type=float, default=None, help="止损百分比（覆盖 config，如 0.20）")
    parser.add_argument("--p-up-buy", dest="p_up_buy", type=float, default=None,
                        help="P(up) 买入阈值（覆盖 config，如 0.60）")
    parser.add_argument("--p-down-buy", dest="p_down_buy", type=float, default=None,
                        help="P(up) 卖出/做空阈值（覆盖 config，如 0.40）")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
