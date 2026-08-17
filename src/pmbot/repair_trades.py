"""历史交易盈亏修复：从 Polymarket 真实成交重算 trades.csv 的 pnl。

背景：旧版实盘盈亏用「钱包余额差值」（平仓时余额 − 开仓时余额），
卖出成交后资金未到账即查询余额 → 止盈也记成 ≈ -买入成本 的假亏损
（如 -1.03 / -1.06）。本工具以 Polymarket CLOB 成交记录为准：

- token_id 从 bot 日志提取（每窗口买入前必有 GET /book?token_id= 记录；
  gamma 查不到已结算历史窗口市场）
- 按 token + 时间窗拉取该 token 的成交（CLOB /trades after/before）
- owner 从买入成交提取（本账户 uuid），过滤出本账户全部成交
- 买入成本 = Σ(BUY price×size)，卖出收入 = Σ(SELL price×size)
- 重算 pnl = 卖出收入 − 买入成本，exit_price 修正为真实成交加权价

买入股数与记录差异 >5% 视为窗口内混入他人成交，跳过并警告。
settle（窗口到期结算兑付）无卖出成交，理论价差即真实收益，跳过。

用法: uv run python -m pmbot.repair_trades --data-dir data_live [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from pmbot.clob_executor import ClobExecutor

SELL_REASONS = {"take_profit", "stop_loss", "sell"}  # 有卖出订单的交易
BOOK_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*book\?token_id=(\d{20,})")


def _extract_token(log_path: Path, before_ts: float) -> str | None:
    """从 bot 日志提取 before_ts（epoch 秒）前最近一次盘口查询的 token_id。

    日志时间戳为本地时区（naive strptime + timestamp() 按本地解释），
    before_ts 为 UTC epoch——两者 epoch 可比。
    """
    token = None
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = BOOK_RE.search(line)
                if not m:
                    continue
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                if ts <= before_ts:
                    token = m.group(2)
                else:
                    break
    except OSError:
        return None
    return token


def _fetch_window_trades(executor: ClobExecutor, asset_id: str,
                         after: float, before: float) -> list[dict]:
    """拉取某 token 在 [after, before] 内的成交（CLOB 时间窗过滤）。"""
    return executor.fetch_token_trades(asset_id, int(after), int(before))


def repair_row(executor: ClobExecutor, log_path: Path, row: dict) -> dict | None:
    """重算单笔交易；返回 {pnl, exit_price} 覆盖字段；无法修复返回 None。"""
    reason = row["reason"]
    if reason not in SELL_REASONS:
        return None
    try:
        win_start = int(row["window_start"])
        ts = float(datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).timestamp())
        size = float(row["size"])
    except (KeyError, ValueError, TypeError):
        return None
    token = _extract_token(log_path, ts)
    if token is None:
        return None
    trades = _fetch_window_trades(executor, token, win_start - 300, ts + 300)
    buys = [t for t in trades if t.get("side") == "BUY"]
    sells = [t for t in trades if t.get("side") == "SELL"]
    if not buys or not sells:
        return None
    # 本账户 owner：买入成交的众数（买入必为本账户市价单）
    owner = Counter(t.get("owner") for t in buys).most_common(1)[0][0]
    if not owner:
        return None
    mine = [t for t in buys + sells if t.get("owner") == owner]
    my_buys = [t for t in mine if t.get("side") == "BUY"]
    my_sells = [t for t in mine if t.get("side") == "SELL"]
    if not my_buys or not my_sells:
        return None
    # 校验买入股数与记录一致（防窗口内他人成交混入）
    bought = sum(float(t["size"]) for t in my_buys)
    if abs(bought - size) / size > 0.05:
        return None
    cost = sum(float(t["price"]) * float(t["size"]) for t in my_buys)
    proceeds = sum(float(t["price"]) * float(t["size"]) for t in my_sells)
    sold = sum(float(t["size"]) for t in my_sells)
    return {
        "pnl": round(proceeds - cost, 6),
        "exit_price": round(proceeds / sold, 6),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 Polymarket 真实成交重算历史交易盈亏")
    parser.add_argument("--data-dir", default="data", help="数据目录（实盘用 data_live）")
    parser.add_argument("--log", default="logs/bot.log", help="bot 日志路径（提取 token_id）")
    parser.add_argument("--dry-run", action="store_true", help="只打印修复结果，不写回")
    args = parser.parse_args(argv)

    trades_path = Path(args.data_dir) / "trades.csv"
    log_path = Path(args.log)
    if not trades_path.is_file():
        print(f"错误：{trades_path} 不存在")
        return 2

    executor = ClobExecutor()
    with open(trades_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fixed = skipped = 0
    for row in rows:
        old_pnl = row["pnl"]
        try:
            fix = repair_row(executor, log_path, row)
        except Exception as e:  # 单笔失败不中断整体
            print(f"  ⚠ {row['ts']} {row['direction']} 修复失败：{e}")
            fix = None
        if fix is None:
            print(f"  跳过 {row['ts']} {row['direction']:5s} {row['reason']:11s} "
                  f"盈亏 {old_pnl}（无成交/不可修复）")
            skipped += 1
            continue
        row["pnl"] = str(fix["pnl"])
        row["exit_price"] = str(fix["exit_price"])
        print(f"  修复 {row['ts']} {row['direction']:5s} {row['reason']:11s} "
              f"盈亏 {old_pnl} → {fix['pnl']:+.6f}（出场 {fix['exit_price']}）")
        fixed += 1

    print(f"\n共 {len(rows)} 笔：修复 {fixed}，跳过 {skipped}")
    if args.dry_run or fixed == 0:
        print("（--dry-run：未写回）" if args.dry_run else "（无改动）")
        return 0
    with open(trades_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"已写回 {trades_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
