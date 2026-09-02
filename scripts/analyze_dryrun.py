#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分析 dry-run 运行日志: 按天统计交易/盈亏/异常。"""
import re, sys
from collections import defaultdict, Counter
from datetime import datetime

LOG = r"E:\code\polymarket-kronos\logs\bot.log"
DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+ (INFO|WARNING|ERROR|CRITICAL) (\S+): (.*)$")
# 只分析最近几天(dry-run BTC 阶段, 8-29 起)
FOCUS = ["2026-08-29", "2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"]

def main():
    days = defaultdict(lambda: {
        "buy": 0, "buy_skip_no_quote": 0,
        "close_tp": 0, "close_sl": 0, "close_exit_loss": 0, "close_expire": 0, "close_other": 0,
        "pnl": 0.0, "pnl_list": [], "fuse": 0, "fuse_msgs": [],
        "errors": Counter(), "warnings": Counter(),
        "signals": Counter(), "started": 0,
    })
    total_lines = 0
    with open(LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = DAY_RE.match(line)
            if not m:
                continue
            d, t, lvl, comp, msg = m.groups()
            if d not in FOCUS:
                continue
            total_lines += 1
            day = days[d]
            if "主循环启动" in msg:
                day["started"] += 1
            if "信号:" in msg:
                m2 = re.search(r"信号:\s*(\w+)\s*\(P\(up\)=([\d.]+)\)", msg)
                if m2:
                    day["signals"][(m2.group(1), m2.group(2))] += 1
            if "市价买入" in msg:
                if "跳过" in msg:
                    if "盘口无报价" in msg:
                        day["buy_skip_no_quote"] += 1
                else:
                    day["buy"] += 1
            if "平仓" in msg:
                pm = re.search(r"盈亏\s*([+-]?[\d.]+)", msg)
                pnl = float(pm.group(1)) if pm else 0.0
                day["pnl"] += pnl
                day["pnl_list"].append((t, msg.strip(), pnl))
                if "take_profit" in msg: day["close_tp"] += 1
                elif "stop_loss" in msg: day["close_sl"] += 1
                elif "exit_loss" in msg: day["close_exit_loss"] += 1
                elif "到期" in msg or "结算" in msg or "窗口结束" in msg: day["close_expire"] += 1
                else: day["close_other"] += 1
            if "熔断" in msg and lvl == "WARNING":
                day["fuse"] += 1
                day["fuse_msgs"].append((t, msg.strip()))
            if lvl in ("ERROR", "WARNING"):
                # 归一化组件+消息(去掉数字)
                norm = re.sub(r"\d+", "N", msg)[:80]
                day[lvl.lower() + "s"][(comp, norm)] += 1

    print(f"总行数(焦点区间): {total_lines}")
    print("=" * 100)
    for d in FOCUS:
        day = days[d]
        if day["started"] == 0 and day["buy"] == 0 and not day["pnl_list"]:
            print(f"\n### {d}  (无记录)")
            continue
        print(f"\n### {d}  启动次数={day['started']}")
        print(f"  买入 {day['buy']} 笔 | 盘口无报价跳过 {day['buy_skip_no_quote']} 次")
        print(f"  平仓 {sum([day['close_tp'],day['close_sl'],day['close_exit_loss'],day['close_expire'],day['close_other']])} 笔 "
              f"(止盈 {day['close_tp']} / 止损 {day['close_sl']} / exit_loss {day['close_exit_loss']} / 到期/结算 {day['close_expire']} / 其他 {day['close_other']})")
        print(f"  当日盈亏 {day['pnl']:+.4f} USDC  熔断 {day['fuse']} 次")
        if day["signals"]:
            up = sum(v for (k, _), v in day["signals"].items() if k == "up")
            dn = sum(v for (k, _), v in day["signals"].items() if k == "down")
            print(f"  信号 up={up} down={dn}")
        if day["fuse_msgs"]:
            for t, m in day["fuse_msgs"]:
                print(f"    [熔断] {t} {m}")
        print("  近期平仓明细(最后5笔):")
        for t, m, p in day["pnl_list"][-5:]:
            print(f"    {t} {m}")
        print("  ERROR 类型 Top:")
        for (comp, n), c in day["errors"].most_common(6):
            print(f"    {c:5d}  {comp}: {n}")
        print("  WARNING 类型 Top(已脱敏):")
        for (comp, n), c in day["warnings"].most_common(8):
            print(f"    {c:5d}  {comp}: {n}")

if __name__ == "__main__":
    sys.exit(main())
