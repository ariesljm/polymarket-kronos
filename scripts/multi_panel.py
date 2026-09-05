"""多标的聚合终端面板：BTC + ETH + SOL 的 momentum 实时状态 + 交易汇总。

用法: uv run python scripts/multi_panel.py [--data-dir data_multi/btc,data_multi/eth,data_multi/sol] [--live]
循环多个数据目录,每 2 秒刷新。视图构建复用 panel_view.build_multi_view
（与 monitor 共用同一读面;模式/目录经 RuntimePaths 派生,不再本脚本硬编码）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pmbot.ledger import load_records  # noqa: E402
from pmbot.panel_view import build_multi_view  # noqa: E402
from pmbot.paths import RuntimePaths  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REFRESH_SEC = 2.0


def _fmt_ts(ts: str) -> str:
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(None).strftime("%H:%M")
    except Exception:
        return (ts or "")[:5]


def block_for(v, data_dir: str | Path, data_root: Path) -> str:
    lines = [f"===== {v.symbol or '?'} ====="]
    if v.strategy_state:
        lines.append(f"  {v.strategy_state}")
    if v.signal:
        d = v.signal["direction"].upper()
        p = v.signal["p_up"]
        rem = f"  剩余{v.window_remaining_sec}s" if v.window_remaining_sec is not None else ""
        lines.append(f"  窗口 {v.window_label}  信号: {d}(P={p:.2f}){rem}")
    if v.position:
        p = v.position
        lines.append(f"  持仓: {p['direction'].upper()} {p['size']:.2f}股 @{p['entry_price']*100:.0f}分")
    if v.settle_pending:
        sp = v.settle_pending
        lines.append(f"  待结算: {sp['direction'].upper()}")
    if v.today_stats:
        ts = v.today_stats
        lines.append(f"  今日: {v.today_pnl:+.2f} USDC ({ts['n']}笔 胜率{ts['wins']/ts['n']:.0%})")
    for t in load_records(data_root / data_dir)[-3:]:
        lines.append(
            f"  {_fmt_ts(t.ts)} {t.direction.value.upper() if hasattr(t.direction, 'value') else str(t.direction).upper():4} "
            f"入{t.entry_price*100:.0f}分 出{t.exit_price*100:.0f}分 {t.pnl:+.2f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多标的聚合面板")
    parser.add_argument("--data-dir", default="data_multi/btc,data_multi/eth,data_multi/sol",
                        help="逗号分隔的数据目录列表（每标的一个 bot 进程一个）")
    parser.add_argument("--live", action="store_true", help="实盘模式（默认 dry-run；模式/目录派生与 monitor 一致）")
    args = parser.parse_args(argv)

    dirs = [d.strip() for d in args.data_dir.split(",") if d.strip()]
    paths_list = [RuntimePaths(data_dir=d, mode="live" if args.live else "dry-run") for d in dirs]

    from pmbot.ledger import load_records
    from rich.live import Live

    with Live(refresh_per_second=1 / REFRESH_SEC, screen=False) as live:
        while True:
            views = build_multi_view(paths_list, str(ROOT / "config.yaml"))
            parts = [block_for(v, d, ROOT) for v, d in zip(views, dirs)]
            total = sum(t.pnl for d in dirs for t in load_records(ROOT / d))
            parts.append(f"===== 累计: {total:+.2f} USDC ===== (Ctrl-C 退出面板, 不影响 bot)")
            live.update("\n".join(parts))
            time.sleep(REFRESH_SEC)
    return 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)
    try:
        main()
    except KeyboardInterrupt:
        pass