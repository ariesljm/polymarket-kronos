"""多标的聚合终端面板（TUI）：各标的实时状态 + 项目汇总。

标的目录默认从 config.yaml 的 momentum.symbols 派生（data_multi/<sym>），
与 bot 标的池单一事实源对齐；可用 --data-dir 覆盖。

视觉参考 corridor-watch（Polymarket 监控终端）：蓝色边框 + 标题栏 +
配置行 + 分隔线标的块 + 持仓表 + 底部状态栏。内容全部中文化。

用法: uv run python scripts/multi_panel.py [--data-dir data_multi/eth,data_multi/sol] [--live]
循环读取各标的数据目录,每 2 秒刷新。视图构建复用 panel_view.build_multi_view
（与 monitor 共用同一读面;模式/目录经 RuntimePaths 派生）。
start_multi.bat 启动 bot 后自动进入本面板；Ctrl-C 停止 bot 并退出面板。
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
    EXIT_LABELS,
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
CARD_MIN_COL_W = 38  # 卡片最小外宽（内容 ~32 + 边框/内边距/列间距）：低于此值内容会折行
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


def _cfg_line(symbols: int, interval: str, amount: float,
              max_entry: float, views: list, online_flags: list[bool]) -> Table:
    """配置行（全局参数，只显示一次，标的块不再重复）+ 真实聚合 WS 连接状态（替代早期硬编码死文案）。

    穿越阈值分标的各异（threshold_by_symbol），不在此显示全局默认值，改在各标的块首行。
    WS 聚合必须与在线判定同源：bot 停机后 status.json 仍留着上次的 connected，
    不查新鲜度会显示假“已连”（与底部“离线”自相矛盾）。
    """
    left = Text(
        f"配置 标的 {symbols} · 每注 {amount:.0f} · 持有至结算 · {interval} · "
        f"入场上限 {max_entry:.2f}",
        style="dim",
    )

    def ws_agg(key: str, label: str) -> Text:
        """聚合 WS 状态：仅在线标的计入；全离线=✖离线 / 部分离线单列 N。"""
        total = len(views)
        offline = sum(1 for on in online_flags if not on)
        if total == 0:
            return Text(f"{label}?", style="dim")
        if offline == total:
            return Text(f"{label}✖离线", style="dim")
        conn = sum(
            1 for v, on in zip(views, online_flags)
            if on and v.ws_status and v.ws_status.get(key) == "connected"
        )
        if offline:
            return Text(f"{label}○{conn}/{total}（{offline}离线）", style="bold yellow")
        if conn == total:
            return Text(f"{label}●已连", style="bold green")
        if conn == 0:
            return Text(f"{label}✖已停", style="dim")
        return Text(f"{label}○{conn}/{total}重连", style="bold yellow")

    right = Text("config.yaml  ", style="dim")
    right.append(ws_agg("book", "盘口"))
    right.append(Text("  ", style="dim"))
    right.append(ws_agg("ticker", "币安WS"))
    bar = Table(expand=True, box=None, pad_edge=False, show_edge=False, padding=0, show_header=False)
    bar.add_column(justify="left", no_wrap=True)
    bar.add_column(justify="right", no_wrap=True, min_width=22)
    bar.add_row(left, right)
    return bar


def _card(v: PanelView, online: bool, threshold: float | None) -> Panel:
    """单标的卡片：2 行内容 + 边框（4 卡/行也能读，标的信息一目了然）。

    标题行 = 标的 + 分标的穿越阈值（阈值为 spec 单一事实源回落）
    行1 = 现货价/偏离 + 涨跌概率；行2 = 策略状态 + 持仓/挂单 + 今日盈亏 + 连亏 + WS
    """
    body: list = []

    # 行1：现货价 + 窗口内偏离 + 涨/跌盘口
    l1 = Text()
    spot = parse_strategy_spot(v.strategy_state)
    if spot:
        price, dev = spot
        l1.append(Text(f"${price:,.2f}", style="bold cyan"))
        l1.append(Text(
            f" {'▲' if dev >= 0 else '▼'}{abs(dev):.2f}%",
            style="green" if dev >= 0 else "red",
        ))
    else:
        l1.append(Text("$—", style="dim"))
    prices = v.prices or {}
    up = prices.get("up_ask")
    down = prices.get("down_ask")
    l1.append(Text("  涨", style="dim"))
    l1.append(Text(_fmt_cents(up) + "¢", style="bold green") if up is not None else Text("—", style="dim"))
    l1.append(Text(" 跌", style="dim"))
    l1.append(Text(_fmt_cents(down) + "¢", style="bold red") if down is not None else Text("—", style="dim"))
    body.append(l1)

    # 行2：持仓/挂单置首 + 反色高亮，**空仓显式写出**（原先只在有仓时显示文字，
    # 无仓时靠"没有文字"推断 → 看不出是否持仓）+ 今日盈亏 + 连亏 + WS
    # （持仓/挂单时不再重复策略状态，保证 4 卡/行不折行；明细交给历史表与底部汇总）
    l2 = Text()
    if v.position:
        p = v.position
        l2.append(Text(f"持{p['direction'].upper()} {p['size']:.1f}", style="bold black on yellow"))
    elif v.pending:
        p = v.pending
        l2.append(Text(f"挂{p['direction'].upper()}", style="bold black on magenta"))
    else:
        l2.append(Text("空仓", style="dim"))
        st = status_tail(v.strategy_state) or ""
        if "已穿越" in st:
            st = "🔺UP" if "UP" in st.upper() else "🔻DOWN"
        elif "等待穿越" in st:
            st = "⏳等待"
        if st:
            l2.append(Text(" "))
            l2.append(Text(st, style="bold yellow" if "穿" in st or "UP" in st or "DOWN" in st else "dim"))
    l2.append(Text(" 今", style="dim"))
    l2.append(_pnl_text(v.today_pnl))
    if v.consecutive_losses:
        l2.append(Text(f" 亏{v.consecutive_losses}", style="dim"))
    ws = v.ws_status or {}
    if ws:
        l2.append(Text(" ", style="dim"))
        for key, label in (("book", "盘"), ("ticker", "币")):
            # 离线时 status.json 陈旧：一律显 ✖（防上次运行的 connected 残留）
            s = ws.get(key) if online else None
            if s == "connected":
                l2.append(Text(f"{label}●", style="bold green"))
            elif s == "reconnecting":
                l2.append(Text(f"{label}○", style="bold yellow"))
            else:
                l2.append(Text(f"{label}✖", style="dim"))
    body.append(l2)
    if v.paused:
        body.append(Text(f"⚠ {v.pause_reason or '熔断'}", style="bold red"))

    title = f"{v.symbol or '?'} ±{threshold:.2f}%" if threshold is not None else f"{v.symbol or '?'}"
    return Panel(
        Group(*body), title=title, title_align="left",
        border_style="green" if online else "red", padding=(0, 1),
    )


def cards_per_row(width: int) -> int:
    """卡片列数：按实测内容宽度定档（防折行）。

    卡内容最宽行 ~32 列（长价位 + 涨/跌盘口），加边框/内边距/列间距 → 每卡需 38 列：
    4 卡/行需 ≥152 / 3 卡 ≥114 / 2 卡 ≥76 / 否则 1 卡。
    """
    for n in (4, 3, 2):
        if width // n >= CARD_MIN_COL_W:
            return n
    return 1


def _symbol_cards(views: list[PanelView], thresholds: dict[str, float],
                  online_flags: list[bool], width: int) -> Table:
    """标的卡片网格：cards_per_row 列自适应，行内等高（空白补位）。"""
    per_row = cards_per_row(width)
    grid = Table(expand=True, box=None, pad_edge=False, show_edge=False,
                 show_header=False, padding=(0, 1))
    for _ in range(per_row):
        grid.add_column(justify="left", ratio=1, no_wrap=False)
    cards = [_card(v, on, thresholds.get(v.symbol)) for v, on in zip(views, online_flags)]
    for i in range(0, len(cards), per_row):
        chunk: list = cards[i:i + per_row]
        chunk += [Text("")] * (per_row - len(chunk))  # 末行补空位，卡片宽度不被拉伸
        grid.add_row(*chunk)
    return grid


def _history_table(records_by_dir: dict[str, list], max_rows: int = 12) -> Table:
    """交易历史表：全量 trades.csv（不再受视图 RECENT_LIMIT 截断），按时间倒序。

    行数 = 终端剩余高度（max_rows）——每标的保底 2 笔后其余额度按全局时间倒序填充，
    避免单一活跃标的把其它标的挤出表外。
    """
    table = Table(expand=True, box=None, pad_edge=False, show_edge=False,
                  header_style="bold white", padding=(0, 1))
    table.add_column("时间", justify="left", style="dim", no_wrap=True, width=12)
    table.add_column("标的", justify="left", no_wrap=True, width=5)
    table.add_column("方向", justify="left", no_wrap=True, width=4)
    table.add_column("数量(股)", justify="right", no_wrap=True, width=9)
    table.add_column("入场", justify="right", no_wrap=True, width=6)
    table.add_column("出场", justify="right", no_wrap=True, width=6)
    table.add_column("盈亏", justify="right", no_wrap=True, width=7)
    table.add_column("原因", justify="left", no_wrap=True, ratio=1, min_width=8)

    def dir_name(d: str) -> str:
        return "涨" if d.upper() == "UP" else ("跌" if d.upper() == "DOWN" else d)

    rows: list[tuple[str, object]] = []
    for recs in records_by_dir.values():
        for r in recs:
            rows.append((r.ts, r))
    rows.sort(key=lambda x: x[0], reverse=True)  # ISO 时间倒序：最新在上

    if not rows:
        table.add_row("", "暂无交易", "", "", "", "", "", "")
        return table

    # 每标的保底 2 笔（公平）+ 其余按时间倒序填充（最新最多）
    MIN_PER_SYMBOL = 2
    picked: list[tuple[str, object]] = []
    chosen: set[int] = set()
    per: dict[str, int] = {}
    for item in rows:
        sym = item[1].symbol
        if per.get(sym, 0) < MIN_PER_SYMBOL:
            per[sym] = per.get(sym, 0) + 1
            picked.append(item)
            chosen.add(id(item[1]))
    for item in rows:
        if len(picked) >= max_rows:
            break
        if id(item[1]) not in chosen:
            picked.append(item)
            chosen.add(id(item[1]))
    picked.sort(key=lambda x: x[0], reverse=True)
    picked = picked[:max_rows]

    for ts, r in picked:
        # 原因中文化：复用 panel_view.EXIT_LABELS（展示文案单一事实源，与面板状态同源）
        label = EXIT_LABELS.get(r.reason, r.reason)
        table.add_row(
            f"{ts[5:16].replace('T', ' ')}", r.symbol or "?", dir_name(r.direction),
            f"{r.size:.2f}", _fmt_cents(r.entry_price), _fmt_cents(r.exit_price),
            _pnl_text(r.pnl), label,
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
        default=None,
        help="逗号分隔的数据目录列表（默认从 config.yaml symbols 派生 data_multi/<sym>）",
    )
    parser.add_argument("--live", action="store_true", help="实盘模式（默认 dry-run；模式/目录派生与 monitor 一致）")
    args = parser.parse_args(argv)

    from pmbot.config import load_config

    # config 先加载：目录派生与配置摘要共用（单一事实源，加标的零改动）
    cfg_loaded = None
    try:
        cfg_loaded = load_config(str(ROOT / "config.yaml"))
    except Exception:
        pass

    if args.data_dir:
        dirs = [d.strip() for d in args.data_dir.split(",") if d.strip()]
    elif cfg_loaded is not None:
        dirs = [f"data_multi/{s.lower()}" for s in cfg_loaded.symbols]
    else:
        dirs = ["data_multi/eth", "data_multi/sol"]
    paths_list = [RuntimePaths(data_dir=d, mode="live" if args.live else "dry-run") for d in dirs]
    mode = "live" if args.live else "dry-run"

    # 配置摘要（全局参数，只显示一次）
    symbols = len(dirs)
    interval = "5m"
    amount = 1.0
    threshold_pct = 0.08
    max_entry = 0.65
    if cfg_loaded is not None:
        symbols = len(cfg_loaded.symbols)
        interval = cfg_loaded.market_interval
        amount = cfg_loaded.amount_per_trade
        threshold_pct = getattr(cfg_loaded, "threshold_pct", 0.08)
        max_entry = getattr(cfg_loaded, "max_entry_price", 0.65)
    # 分标的穿越阈值：threshold_by_symbol 覆盖 + 全局默认兑底（YAML 键为字符串）
    global_threshold = threshold_pct
    tbs = getattr(cfg_loaded, "threshold_by_symbol", {}) if cfg_loaded else {}
    thresholds = {
        s: float(tbs.get(s, global_threshold))
        for s in (cfg_loaded.symbols if cfg_loaded else [d.split("/")[-1].upper() for d in dirs])
    }

    global _checks
    with Live(refresh_per_second=1 / REFRESH_SEC, screen=False, console=console) as live:
        while True:
            _checks += 1
            # cfg 一次加载复用（build_live_view 不再每标的每 2s 重复解析 config.yaml）
            views = build_multi_view(paths_list, str(ROOT / "config.yaml"), cfg=cfg_loaded)
            per_dir_trades = {d: load_records(ROOT / d) for d in dirs}
            online_flags = [_is_online(ROOT / d) for d in dirs]

            # 高度预算：固定开销(边框/标题/配置/分隔/表头/汇总/状态/操作) + 卡片行数,
            # 剩余全部给交易历史（行数随终端高度自适应，不再固定 3 笔/标的）
            card_lines = ((len(views) + cards_per_row(console.width) - 1)
                          // cards_per_row(console.width)) * 4
            max_rows = max(3, console.height - 11 - card_lines)

            body = Group(
                _title_bar(views, mode, interval),
                _cfg_line(symbols, interval, amount, max_entry, views, online_flags),
                Text("─" * max(10, console.width - 4), style="dim"),
                _symbol_cards(views, thresholds, online_flags, console.width),
                Text("─" * max(10, console.width - 4), style="dim"),
            )

            body.renderables.append(_history_table(per_dir_trades, max_rows))
            body.renderables.append(_deploy_line(amount, symbols, per_dir_trades))
            body.renderables.append(Text("─" * max(10, console.width - 4), style="dim"))
            body.renderables.append(_status_bar(views, online_flags))
            body.renderables.append(Text("Ctrl-C 停止 bot 并退出", style="dim"))

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
