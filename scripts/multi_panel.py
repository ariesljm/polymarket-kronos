"""多标的聚合终端面板（TUI）：BTC + ETH + SOL 实时状态 + 项目汇总。

视觉参考 corridor-watch（Polymarket 监控终端）：蓝色边框 + 标题栏 +
配置行 + 分隔线标的块 + 持仓表 + 底部状态栏。内容全部中文化。

用法: uv run python scripts/multi_panel.py [--data-dir data_multi/btc,data_multi/eth,data_multi/sol] [--live]
循环读取各标的数据目录,每 2 秒刷新。视图构建复用 panel_view.build_multi_view
（与 monitor 共用同一读面;模式/目录经 RuntimePaths 派生）。
start_multi.bat 启动 bot 后自动进入本面板；Ctrl-C 退出面板不影响 bot。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pmbot.ledger import load_records  # noqa: E402
from pmbot.panel_view import (  # noqa: E402
    PanelView,
    build_multi_view,
    parse_strategy_spot,
    status_tail,
    _fmt_cents,
)
from pmbot.paths import RuntimePaths  # noqa: E402

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

console = Console()
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


def _win_pct(stats: dict | None) -> str:
    if not stats or not stats.get("n"):
        return "—"
    return f"{stats['wins'] / stats['n']:.0%}"


def _pnl_text(pnl: float) -> Text:
    return Text(f"{pnl:+.2f}", style="green" if pnl > 0 else ("red" if pnl < 0 else "white"))


def _is_online(data_dir: str | Path) -> bool:
    """status.json 最近更新时间判定 bot 在线（> OFFLINE_GRACE_SEC 未更新 = 离线）。"""
    try:
        p = Path(data_dir) / "status.json"
        return p.is_file() and (time.time() - p.stat().st_mtime) < OFFLINE_GRACE_SEC
    except Exception:
        return False


# ---- 渲染 ----

def _title_bar(views: list[PanelView], mode: str, interval: str) -> Table:
    """标题栏：程序名 | 市场描述 | 右侧统一窗口信息（三个标的同时刻,只显示一次）。"""
    win = "窗口 — 剩 —"
    if views:
        label = views[0].window_label  # "09-05 16:40-16:45"
        rem = views[0].window_remaining_sec
        rem_txt = f"{rem // 60}分{rem % 60:02d}秒" if rem is not None else "—"
        win = f"窗口 {label} 剩 {rem_txt}"
    t = Text("PMBOT", style="bold white")
    t.append("  ", style="dim")
    t.append(f"Polymarket {interval} 涨跌 momentum", style="cyan")
    t.append(f"  {'实盘' if mode == 'live' else 'dry-run 模拟'}",
             style="bold red" if mode == "live" else "bold yellow")
    right = Text(win, style="dim")
    bar = Table(expand=True, box=None, pad_edge=False, show_edge=False, padding=0, show_header=False)
    bar.add_column(justify="left", no_wrap=True)
    bar.add_column(justify="right", no_wrap=True, min_width=24)
    bar.add_row(t, right)
    return bar


def _cfg_line(symbols: int, interval: str, amount: float, threshold_pct: float,
              max_entry: float, views: list) -> Table:
    """配置行（全局参数，只显示一次，标的块不再重复）+ 真实聚合 WS 连接状态（替代早期硬编码死文案）。"""
    left = Text(
        f"配置 标的 {symbols} · 每注 {amount:.0f} · 持有至结算 · {interval} · "
        f"穿越±{threshold_pct:.2f}% · 入场上限 {max_entry:.2f}",
        style="dim",
    )

    def ws_agg(key: str, label: str) -> Text:
        """聚合全部标的的 WS 状态:全连=●已连 / 部分=○N/M重连 / 全停=✖已停。"""
        sts = [v.ws_status.get(key) for v in views if v.ws_status and v.ws_status.get(key)]
        if not sts:
            return Text(f"{label}?", style="dim")
        if all(s == "connected" for s in sts):
            return Text(f"{label}●已连", style="bold green")
        if all(s == "stopped" for s in sts):
            return Text(f"{label}✖已停", style="dim")
        n = sts.count("connected")
        return Text(f"{label}○{n}/{len(sts)}重连", style="bold yellow")

    right = Text("config.yaml  ", style="dim")
    right.append(ws_agg("book", "盘口"))
    right.append(Text("  ", style="dim"))
    right.append(ws_agg("ticker", "币安WS"))
    bar = Table(expand=True, box=None, pad_edge=False, show_edge=False, padding=0, show_header=False)
    bar.add_column(justify="left", no_wrap=True)
    bar.add_column(justify="right", no_wrap=True, min_width=22)
    bar.add_row(left, right)
    return bar


def _symbol_block(v: PanelView, online: bool) -> Group:
    """单标的块：现货价 / 涨跌概率 / 策略状态 / WS 状态 / 持仓 / 统计（只显示标特定的内容，窗口信息统一在顶部栏）。"""
    lines: list = []

    # 首行：在线灯 + 标的 + 现货价 + 涨跌（窗口信息统一在顶部栏显示）
    head = Text("● " if online else "○ ", style="green" if online else "red")
    head.append(Text(f"{v.symbol or '?'}  ", style="bold white"))

    spot = parse_strategy_spot(v.strategy_state)
    if spot:
        price, dev = spot
        arrow = "▲" if dev >= 0 else "▼"
        head.append(Text(f"${price:,.2f} ", style="bold cyan"))
        head.append(Text(f"{arrow}{abs(dev):.2f}%", style="green" if dev >= 0 else "red"))
    else:
        head.append(Text("$—", style="dim"))
    lines.append(head)

    # 第二行：涨/跌概率 + 策略状态
    prices = v.prices or {}
    up = prices.get("up_ask")
    down = prices.get("down_ask")
    probs = Text("涨 ", style="dim")
    probs.append(Text(_fmt_cents(up) + "¢", style="bold green") if up is not None else Text("—", style="dim"))
    probs.append(Text("  跌 ", style="dim"))
    probs.append(Text(_fmt_cents(down) + "¢", style="bold red") if down is not None else Text("—", style="dim"))
    st = status_tail(v.strategy_state)
    if st:
        probs.append(Text(f"  状态: {st}", style="dim"))
    lines.append(probs)

    # WS 连接状态（数据链路健康：盘口 book_sampler + 币安 ticker;●=已连 ○=重连中 ✖=已停）
    ws = v.ws_status or {}
    if ws:
        ws_line = Text("WS ", style="dim")
        for key, label in (("book", "盘口"), ("ticker", "币安")):
            s = ws.get(key)
            if s == "connected":
                lit = Text(f"{label}●", style="bold green")
            elif s == "reconnecting":
                lit = Text(f"{label}○", style="bold yellow")
            else:
                lit = Text(f"{label}✖", style="dim")
            ws_line.append(lit)
            ws_line.append(Text("  ", style="dim"))
        lines.append(ws_line)

    # 持仓 / 挂单
    if v.position:
        p = v.position
        lines.append(Text(
            f"持仓 {p['direction'].upper()} {p['size']:.2f}股 @{_fmt_cents(p['entry_price'])}分",
            style="bold yellow",
        ))
    elif v.pending:
        p = v.pending
        lines.append(Text(
            f"挂单 {p['direction'].upper()} {p['size'] or '?'}股 @{_fmt_cents(p['price'])}分",
            style="bold magenta",
        ))
    else:
        lines.append(Text("持仓 —", style="dim"))
    if v.paused:
        lines.append(Text(f"⚠ 已暂停: {v.pause_reason or '熔断'}", style="bold red"))

    # 统计行
    ts = v.today_stats
    rs = v.recent_stats
    stat = Text("今日 ")
    stat.append(_pnl_text(v.today_pnl))
    stat.append(f" ({ts['n']}笔 {_win_pct(ts)})" if ts else " (—)")
    stat.append(Text("  累计 "))
    if rs:
        stat.append(_pnl_text(rs["pnl"]))
        stat.append(f" ({rs['n']}笔 {_win_pct(rs)})")
    else:
        stat.append("—")
    stat.append(Text(f"  连亏 {v.consecutive_losses}", style="dim"))
    lines.append(stat)

    border = "green" if online else "red"
    return Panel(Group(*lines), title=f"{v.symbol or '?'}", border_style=border, padding=(0, 1))


def _history_table(views: list[PanelView], per_symbol: int = 3) -> Table:
    """交易历史表：各标的最细交易明细，按时间倒序（最新在上）。

    每标的取最近 per_symbol 笔（panel_view.recent_trades 已带 size/label），
    跨标的合并排序——市场/时间/方向/数量/价格/盈亏一目了然。
    """
    table = Table(expand=True, box=None, pad_edge=False, show_edge=False,
                  header_style="bold white", padding=0)
    table.add_column("时间", justify="left", style="dim", no_wrap=True, min_width=5)
    table.add_column("标的", justify="left", no_wrap=True, min_width=4)
    table.add_column("方向", justify="left", no_wrap=True, min_width=2)
    table.add_column("数量(股)", justify="right", no_wrap=True, min_width=7)
    table.add_column("入场", justify="right", no_wrap=True, min_width=5)
    table.add_column("出场", justify="right", no_wrap=True, min_width=5)
    table.add_column("盈亏", justify="right", no_wrap=True, min_width=6)
    table.add_column("原因", justify="left", no_wrap=True, min_width=8)

    def dir_name(d: str) -> str:
        return "涨" if d.upper() == "UP" else ("跌" if d.upper() == "DOWN" else d)

    rows = []
    for v in views:
        for t in (v.recent_trades or [])[:per_symbol]:
            rows.append((t["ts"], v.symbol or "?", t))
    rows.sort(key=lambda r: r[0], reverse=True)  # 时间倒序：最新在上

    if not rows:
        table.add_row("", "暂无交易", "", "", "", "", "", "")
        return table
    for ts, sym, t in rows:
        label = t.get("label") or t.get("reason") or ""
        table.add_row(
            ts, sym, dir_name(t["direction"]),
            f"{t.get('size', 0):.2f}", _fmt_cents(t["entry"]), _fmt_cents(t["exit"]),
            _pnl_text(t["pnl"]), label,
        )
    return table


def _deploy_line(amount: float, symbols: int, per_dir_trades: dict[str, list]) -> Text:
    """底部汇总行：投入 / 累计盈亏 / 胜率 / 统计门限。"""
    all_trades = [t for ts in per_dir_trades.values() for t in ts]
    n = len(all_trades)
    total = sum(t.pnl for t in all_trades)
    wins = sum(1 for t in all_trades if t.pnl > 0)
    t_val = _t_stat(all_trades)

    t = Text("投入 ", style="dim")
    t.append(f"${amount * symbols:,.0f}  ", style="bold")
    t.append("  累计 ")
    t.append(_pnl_text(total))
    t.append(f"  胜率 {wins / n:.0%}" if n else "  胜率 —", style="dim")
    if t_val is not None:
        gate_ok = n >= GATE_TRADES and abs(t_val) > GATE_T
        style = "bold green" if gate_ok else ("bold yellow" if n >= GATE_TRADES else "dim")
        t.append(Text(f"  t={t_val:+.2f}", style=style))
        t.append(Text(f" (门限 {n}/{GATE_TRADES}笔)", style="dim"))
    return t


def _status_bar(views: list[PanelView], online_flags: list[bool]) -> Text:
    """底部状态栏：检查次数 + 各标的涨跌价（映射 corridor-watch 的 [lock] checks）。"""
    t = Text("[锁定]", style="bold yellow")
    t.append(f"  检查 {_checks:,}", style="dim")
    for v, on in zip(views, online_flags):
        prices = v.prices or {}
        up = prices.get("up_ask")
        spot = parse_strategy_spot(v.strategy_state)
        if not on:
            t.append(Text(f"  {v.symbol or '?'} 离线", style="red"))
            continue
        t.append(Text(f"  {v.symbol or '?'} ", style="bold"))
        if up is not None:
            t.append(Text(f"涨{_fmt_cents(up)}¢", style="green"))
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

    # 配置摘要（全局参数，只显示一次）
    symbols = len(dirs)
    interval = "5m"
    amount = 1.0
    threshold_pct = 0.08
    max_entry = 0.65
    from pmbot.config import load_config

    cfg_loaded = None
    try:
        cfg_loaded = load_config(str(ROOT / "config.yaml"))
        symbols = len(cfg_loaded.symbols)
        interval = cfg_loaded.market_interval
        amount = cfg_loaded.amount_per_trade
        threshold_pct = getattr(cfg_loaded, "threshold_pct", 0.08)
        max_entry = getattr(cfg_loaded, "max_entry_price", 0.65)
    except Exception:
        pass

    global _checks
    with Live(refresh_per_second=1 / REFRESH_SEC, screen=False, console=console) as live:
        while True:
            _checks += 1
            # cfg 一次加载复用（build_live_view 不再每标的每 2s 重复解析 config.yaml）
            views = build_multi_view(paths_list, str(ROOT / "config.yaml"), cfg=cfg_loaded)
            per_dir_trades = {d: load_records(ROOT / d) for d in dirs}
            online_flags = [_is_online(ROOT / d) for d in dirs]

            body = Group(
                _title_bar(views, mode, interval),
                _cfg_line(symbols, interval, amount, threshold_pct, max_entry, views),
                Text("─" * max(10, console.width - 4), style="dim"),
            )
            for v, on in zip(views, online_flags):
                body.renderables.append(_symbol_block(v, on))
                body.renderables.append(Text("─" * max(10, console.width - 4), style="dim"))

            body.renderables.append(_history_table(views))
            body.renderables.append(_deploy_line(amount, symbols, per_dir_trades))
            body.renderables.append(Text("─" * max(10, console.width - 4), style="dim"))
            body.renderables.append(_status_bar(views, online_flags))
            body.renderables.append(Text(
                "操作: Ctrl-C 退出面板(不停止 bot) · 停止全部: start_multi.bat stop · "
                "再次观察: uv run python scripts/multi_panel.py",
                style="dim",
            ))

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
