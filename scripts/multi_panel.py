"""多标的聚合终端面板（TUI）：BTC + ETH + SOL 实时状态 + 项目汇总。

视觉参考 corridor-watch（Polymarket 月度障碍监控终端）：蓝色边框 +
标题栏 + 配置行 + 分隔线分隔的标的行情块 + LEGS 持仓表 + 底部状态栏。
内容映射到 pmbot 的 5m Up/Down momentum 场景。

用法: uv run python scripts/multi_panel.py [--data-dir data_multi/btc,data_multi/eth,data_multi/sol] [--live]
循环读取各标的数据目录,每 2 秒刷新。视图构建复用 panel_view.build_multi_view
（与 monitor 共用同一读面;模式/目录经 RuntimePaths 派生）。
start_multi.bat 启动 bot 后自动进入本面板；Ctrl-C 退出面板不影响 bot。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pmbot.ledger import load_records  # noqa: E402
from pmbot.panel_view import PanelView, build_multi_view  # noqa: E402
from pmbot.paths import RuntimePaths  # noqa: E402

from rich.columns import Columns  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REFRESH_SEC = 2.0
OFFLINE_GRACE_SEC = 20.0  # status.json 超过该时长未更新 → 判定离线
GATE_TRADES = 200  # 统计门限：样本量
GATE_T = 1.96  # 统计门限：|t|
TRAIL_LEN = 120  # 轨迹采样点数（@2s ≈ 4 分钟）
SEP = "─" * 60

console = Console()
_SPOT_RE = re.compile(r"基准\s+([\d,.]+)\s+偏离\s+([+-][\d.]+)%")
_SPARK = "▁▂▃▄▅▆▇█"
_checks = 0  # 刷新计数（底部状态栏 checks）


# ---- 工具 ----

def _t_stat(trades: list) -> float | None:
    """全量 PnL 的 t 统计量（纯 python，不依赖 numpy）。"""
    pnls = [t.pnl for t in trades]
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    if var == 0:
        return None
    return mean / (var / n) ** 0.5


def _fmt_ts(ts: str) -> str:
    try:
        return (
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
            .astimezone(None)
            .strftime("%H:%M")
        )
    except Exception:
        return (ts or "")[:5]


def _fmt_cents(x: float) -> str:
    c = x * 100
    return f"{c:.2f}" if c < 1 else f"{c:.0f}"


def _win_pct(stats: dict | None) -> str:
    if not stats or not stats.get("n"):
        return "—"
    return f"{stats['wins'] / stats['n']:.0%}"


def _pnl_text(pnl: float, sign: bool = True) -> Text:
    s = f"{pnl:+.2f}" if sign else f"{pnl:.2f}"
    return Text(s, style="green" if pnl > 0 else ("red" if pnl < 0 else "white"))


def _parse_spot(strategy_state: str | None) -> tuple[float, float] | None:
    """从 momentum 策略状态文案解析（基准价, 偏离%）→ 反推当前现货价。"""
    if not strategy_state:
        return None
    m = _SPOT_RE.search(strategy_state)
    if not m:
        return None
    base = float(m.group(1).replace(",", ""))
    dev = float(m.group(2))
    return base * (1 + dev / 100), dev


def _sparkline(seq: list[float]) -> str:
    """价格轨迹 → 7 级字符图（▁▂▃▄▅▆▇█）。"""
    if len(seq) < 2:
        return ""
    lo, hi = min(seq), max(seq)
    span = hi - lo or 1
    return "".join(_SPARK[min(7, int((x - lo) / span * 7))] for x in seq)


def _read_book(data_dir: str | Path) -> dict:
    """读实时盘口 book.json（过期/缺失返回空 dict）。"""
    try:
        p = Path(data_dir) / "book.json"
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - (data.get("ts", 0) / 1000) > 5.0:
            return {}
        return {k: data[k] for k in ("up_ask", "up_bid", "down_ask", "down_bid") if k in data}
    except Exception:
        return {}


def _is_online(data_dir: str | Path) -> bool:
    """status.json 最近更新时间判定 bot 在线（> OFFLINE_GRACE_SEC 未更新 = 离线）。"""
    try:
        p = Path(data_dir) / "status.json"
        return p.is_file() and (time.time() - p.stat().st_mtime) < OFFLINE_GRACE_SEC
    except Exception:
        return False


# ---- 渲染 ----

def _title_bar(views: list[PanelView], mode: str) -> Table:
    """标题栏（Table 单行两列）：左程序名+市场描述，右 resolve 窗口结束时间。"""
    resolve = "—"
    if views:
        label = views[0].window_label  # "09-05 16:40-16:45"
        if label and "-" in label:
            resolve = label.rsplit("-", 1)[-1].strip()  # "16:45"
    t = Text("PMBOT", style="bold white")
    t.append("  ", style="dim")
    t.append("Polymarket 5m Up/Down momentum", style="cyan")
    t.append(f"  {'实盘' if mode == 'live' else 'dry-run 模拟'}",
             style="bold red" if mode == "live" else "bold yellow")
    right = Text(f"resolve {resolve}", style="dim")
    bar = Table(expand=True, box=None, pad_edge=False, show_edge=False, padding=0, show_header=False)
    bar.add_column(justify="left", no_wrap=True)
    bar.add_column(justify="right", no_wrap=True, min_width=18)
    bar.add_row(t, right)
    return bar


def _cfg_line(symbols: int, interval: str, amount: float) -> Table:
    """配置行（小字）：左 cfg 摘要，右 config 来源/clob/binance。"""
    left = Text(f"cfg sym={symbols} size={amount:.0f} hold=settle hdg=off  {interval}", style="dim")
    right = Text("config=config.yaml  ", style="dim")
    right.append("clob ok  ", style="dim green")
    right.append("binance ws", style="dim green")
    bar = Table(expand=True, box=None, pad_edge=False, show_edge=False, padding=0, show_header=False)
    bar.add_column(justify="left", no_wrap=True)
    bar.add_column(justify="right", no_wrap=True, min_width=30)
    bar.add_row(left, right)
    return bar


def _symbol_block(v: PanelView, online: bool, trail: deque, threshold: float) -> Group:
    """单标的行情块（分隔线分隔）：标的名 + 现货价大字 + UP/DOWN + 偏离 + 剩余 + 轨迹。"""
    # 首行：标的名 | 现货价大字 | 右侧阈值（Table 单行自动宽度）
    name = Text(f"{v.symbol or '?'} ", style="bold white")
    name.append("●" if online else "○", style="green" if online else "red")

    spot = _parse_spot(v.strategy_state)
    price_txt = Text("$—", style="dim")
    if spot:
        price, dev = spot
        arrow = "▲" if dev >= 0 else "▼"
        price_txt = Text(f"${price:,.2f} ", style="bold cyan")
        price_txt.append(f"{arrow}{abs(dev):.2f}%", style="green" if dev >= 0 else "red")

    thresh = Text(f"entry-limit [{threshold:.2f}]", style="dim")
    head = Table(expand=True, box=None, pad_edge=False, show_edge=False, padding=0, show_header=False)
    head.add_column(justify="left", no_wrap=True, min_width=4)
    head.add_column(justify="left", no_wrap=True)
    head.add_column(justify="right", no_wrap=True, min_width=16)
    head.add_row(name, price_txt, thresh)

    # UP/DOWN 概率 + 偏离 + 剩余
    prices = v.prices or {}
    up = prices.get("up_ask")
    down = prices.get("down_ask")
    probs = Text("up ", style="dim")
    if up is not None:
        probs.append(Text(_fmt_cents(up) + "¢", style="bold green"))
    else:
        probs.append("—", style="dim")
    probs.append("  down ", style="dim")
    if down is not None:
        probs.append(Text(_fmt_cents(down) + "¢", style="bold red"))
    else:
        probs.append("—", style="dim")

    rem = v.window_remaining_sec
    rem_txt = f"{rem // 60}m{rem % 60:02d}s" if rem is not None else "—"
    info = Text(f"  dev {spot[1]:+.2f}%  ", style="dim") if spot else Text("  dev —  ", style="dim")
    info.append(f"剩 {rem_txt}", style="dim")

    lines = [head, Group(probs, info)]
    if len(trail) >= 2:
        lines.append(Text(f"  {_sparkline(list(trail))} (近4分钟)", style="dim"))

    if v.paused:
        lines.append(Text(f"  ⚠ 已暂停: {v.pause_reason or '熔断'}", style="bold red"))
    if v.strategy_state:
        # 策略状态（基准/等待穿越）放最小行
        st = v.strategy_state.split(" 基准 ")[0] if " 基准 " in v.strategy_state else v.strategy_state
        lines.append(Text(f"  {st}", style="dim"))

    return Group(*lines)


def _legs_table(views: list[PanelView], per_dir_trades: dict[str, list]) -> Table:
    """LEGS 持仓表：各标的当前持仓 + 最近结算交易（映射 corridor-watch 的 LEGS）。"""
    table = Table(expand=True, box=None, pad_edge=False, show_edge=False,
                  header_style="bold white", padding=0)
    table.add_column("LEGS", justify="left", style="dim", no_wrap=True, min_width=6)
    table.add_column("sym", justify="left", no_wrap=True, min_width=4)
    table.add_column("dir", justify="left", no_wrap=True, min_width=4)
    table.add_column("size", justify="right", no_wrap=True, min_width=5)
    table.add_column("entry", justify="right", no_wrap=True, min_width=5)
    table.add_column("exit", justify="right", no_wrap=True, min_width=5)
    table.add_column("pnl", justify="right", no_wrap=True, min_width=6)

    has_row = False
    for v in views:
        if v.position:
            p = v.position
            table.add_row(
                "> " + (v.symbol or "?"),
                v.symbol or "?",
                p["direction"].upper(),
                f"{p['size']:.2f}",
                _fmt_cents(p["entry_price"]),
                "open",
                Text("—", style="yellow"),
            )
            has_row = True
        if v.pending:
            p = v.pending
            table.add_row(
                "  " + (v.symbol or "?"),
                v.symbol or "?",
                p["direction"].upper(),
                f"{p['size'] or '?'}",
                _fmt_cents(p["price"]),
                "pending",
                Text("—", style="magenta"),
            )
            has_row = True
    # 最近结算交易（每标的最多一笔，按时间倒序）
    for v in views:
        if v.recent_trades:
            t = v.recent_trades[0]
            table.add_row(
                "  " + (v.symbol or "?"),
                v.symbol or "?",
                t["direction"].upper(),
                "—",
                _fmt_cents(t["entry"]),
                _fmt_cents(t["exit"]),
                _pnl_text(t["pnl"]),
            )
            has_row = True
    if not has_row:
        table.add_row("", "no open positions", "", "", "", "", "")
    return table


def _deploy_line(amount: float, symbols: int, per_dir_trades: dict[str, list]) -> Text:
    """底部汇总行（deployed / 累计 PnL / 胜率 / 门限）。"""
    all_trades = [t for ts in per_dir_trades.values() for t in ts]
    n = len(all_trades)
    total = sum(t.pnl for t in all_trades)
    wins = sum(1 for t in all_trades if t.pnl > 0)
    t_val = _t_stat(all_trades)

    t = Text("deployed ", style="dim")
    t.append(f"${amount * symbols:,.0f}  ", style="bold")
    t.append("  累计 ", style="dim")
    t.append(_pnl_text(total))
    t.append(f"  胜率 {wins / n:.0%}" if n else "  胜率 —", style="dim")
    if t_val is not None:
        gate_ok = n >= GATE_TRADES and abs(t_val) > GATE_T
        style = "bold green" if gate_ok else ("bold yellow" if n >= GATE_TRADES else "dim")
        t.append(Text(f"  t={t_val:+.2f}", style=style))
        t.append(Text(f" (门限 {n}/{GATE_TRADES}笔)", style="dim"))
    return t


def _status_bar(views: list[PanelView], online_flags: list[bool]) -> Text:
    """底部状态栏：[lock] checks N  <每标的 up 价+偏离>（映射 corridor-watch）。"""
    t = Text("[lock]", style="bold yellow")
    t.append(f"  checks {_checks:,}", style="dim")
    for v, on in zip(views, online_flags):
        prices = v.prices or {}
        up = prices.get("up_ask")
        spot = _parse_spot(v.strategy_state)
        if not on:
            t.append(Text(f"  {v.symbol or '?'} 离线", style="red"))
            continue
        t.append(Text(f"  {v.symbol or '?'} ", style="bold"))
        if up is not None:
            t.append(Text(f"up{_fmt_cents(up)}¢", style="green"))
        if spot:
            t.append(Text(f" {spot[1]:+.2f}%", style="dim"))
    return t


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多标的聚合面板")
    parser.add_argument(
        "--data-dir",
        default="data_multi/btc,data_multi/eth,data_multi/sol",
        help="逗号分隔的数据目录列表（每标的一个 bot 进程一个）",
    )
    parser.add_argument("--live", action="store_true", help="实盘模式（默认 dry-run；模式/目录派生与 monitor 一致）")
    args = parser.parse_args(argv)

    dirs = [d.strip() for d in args.data_dir.split(",") if d.strip()]
    paths_list = [RuntimePaths(data_dir=d, mode="live" if args.live else "dry-run") for d in dirs]
    mode = "live" if args.live else "dry-run"
    trails: dict[str, deque] = {d: deque(maxlen=TRAIL_LEN) for d in dirs}

    # 配置摘要（symbols/interval/amount/threshold）
    symbols = len(dirs)
    interval = "5m"
    amount = 1.0
    threshold = 0.65
    try:
        from pmbot.config import load_config

        cfg = load_config(str(ROOT / "config.yaml"))
        symbols = len(cfg.symbols)
        interval = cfg.market_interval
        amount = cfg.amount_per_trade
        threshold = getattr(cfg, "max_entry_price", 0.65)
    except Exception:
        pass

    global _checks
    with Live(refresh_per_second=1 / REFRESH_SEC, screen=False, console=console) as live:
        while True:
            _checks += 1
            views = build_multi_view(paths_list, str(ROOT / "config.yaml"))
            per_dir_trades = {d: load_records(ROOT / d) for d in dirs}
            online_flags = [_is_online(ROOT / d) for d in dirs]

            for d in dirs:
                book = _read_book(ROOT / d)
                if "up_ask" in book:
                    trails[d].append(book["up_ask"])

            body = Group(
                _title_bar(views, mode),
                _cfg_line(symbols, interval, amount),
                Text("─" * max(10, console.width - 4), style="dim"),
            )
            for v, on, d in zip(views, online_flags, dirs):
                body.renderables.append(_symbol_block(v, on, trails[d], threshold))
                body.renderables.append(Text("─" * max(10, console.width - 4), style="dim"))

            body.renderables.append(_legs_table(views, per_dir_trades))
            body.renderables.append(_deploy_line(amount, symbols, per_dir_trades))
            body.renderables.append(Text("─" * max(10, console.width - 4), style="dim"))
            body.renderables.append(_status_bar(views, online_flags))

            frame = Panel(body, border_style="blue", padding=(0, 1))
            live.update(frame)
            time.sleep(REFRESH_SEC)
    return 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)
    try:
        main()
    except KeyboardInterrupt:
        pass
