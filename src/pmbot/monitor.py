"""终端监控面板（只读 TUI）：并行于主循环，每 2 秒刷新运行状态。

用法: uv run python -m pmbot.monitor [--status data/status.json]
数据只读自 status.json / trades.csv / PredictionLog，不改主循环。

展示逻辑（build_view / render / PanelView / PanelConfig）见 panel_view.py；
实时价轮询见 spot_price.py；本模块只负责 CLI 入口与 TUI 渲染循环。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

# 展示逻辑与实时价从深模块重导出（保持 pmbot.monitor.X 的旧导入路径兼容）
from pmbot.panel_view import (  # noqa: F401
    EXIT_LABELS,
    RECENT_LIMIT,
    REFRESH_SEC,
    PanelConfig,
    PanelView,
    build_live_view as build_live_view_impl,
    _fmt_cents,
    _read_book_prices,
    build_view,
    render,
)
from pmbot.spot_price import SpotPrice  # noqa: F401


def _build_live_view(symbol, config, paths, recent_limit=None, uptime_sec=None, spot=None):
    """TUI/Web 控制台共用：读取状态/交易/盘口/配置并构建视图。"""
    return build_live_view_impl(symbol, config, paths,
                                recent_limit=recent_limit, uptime_sec=uptime_sec, spot=spot)


def main(argv: list[str] | None = None) -> int:
    from pmbot.web_ui import WEB_PORT

    parser = argparse.ArgumentParser(description="PMBOT 终端监控面板（只读 TUI + Web 控制台）")
    parser.add_argument("--status", default=None, help="状态文件（默认 <data-dir>/status.json）")
    parser.add_argument("--trades", default=None, help="交易日志（默认 <data-dir>/trades.csv）")
    parser.add_argument("--symbol", default=None, help="预测日志标的（默认取 status 的 symbol）")
    parser.add_argument("--log-dir", default=None, help="预测日志目录（默认 <data-dir>）")
    parser.add_argument("--data-dir", default="data", help="数据目录（与主循环 --data-dir 一致）")
    parser.add_argument("--refresh", type=float, default=REFRESH_SEC, help="刷新间隔秒（默认 1.0）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（Web 启动主循环时用）")
    parser.add_argument("--live", dest="live", action="store_true", help="实盘模式（Web 启动主循环时用）")
    parser.add_argument("--dry-run", dest="live", action="store_false", help="模拟模式（默认）")
    parser.set_defaults(live=False)
    parser.add_argument("--web-port", type=int, default=WEB_PORT, help="Web 控制台端口（-1 禁用）")
    parser.add_argument("--web-host", default="0.0.0.0",
                        help="Web 控制台绑定地址（默认 0.0.0.0 局域网可访问；写操作需 token）")
    parser.add_argument("--web-only", action="store_true",
                        help="只跑 Web 控制台，不渲染终端面板（start_bot 默认）")
    args = parser.parse_args(argv)
    # 显式路径参数可覆盖派生；未指定时基于 data-dir 派生（与主循环同目录）
    from pmbot.config import load_config as _load_config
    from pmbot.paths import ProcessControl, RuntimePaths, paths_for

    paths = RuntimePaths(
        data_dir=args.data_dir,
        mode="live" if args.live else "dry-run",
        status=args.status,
        trades=args.trades,
        log_dir=args.log_dir,
    )

    from rich.console import Console
    from rich.live import Live

    import webbrowser

    console = Console()
    console.print("[bold cyan]PMBOT 监控面板启动（Ctrl-C 退出）[/]")
    pc = ProcessControl(show_tui=not args.web_only, live=args.live, paths=paths)
    lock = threading.Lock()
    t_start = time.monotonic()
    server = None
    spot = SpotPrice(symbol=args.symbol or _load_config(args.config).symbols[0])

    def apply_mode(live: bool) -> None:
        """切换面板/启动主循环的运行模式（模拟 ↔ 实盘，含数据目录切换）。

        单一事实源：pc.paths 换新，路径派生与展示模式全部跟随。
        """
        pc.live = live
        pc.paths = paths_for(live)

    if args.web_port > 0:
        import os
        import secrets

        from pmbot.web_ui import WEB_PORT, start_server

        # 写操作访问令牌：优先环境变量，否则读/生成 data/web_token.txt
        token = os.environ.get("PMBOT_WEB_TOKEN", "")
        token_file = Path("data") / "web_token.txt"
        if not token:
            try:
                if token_file.is_file():
                    token = token_file.read_text(encoding="utf-8").strip()
                if not token:
                    token = secrets.token_urlsafe(16)
                    token_file.write_text(token, encoding="utf-8")
            except OSError:
                token = ""

        spot.start()
        server = start_server(
            lambda: _build_live_view(args.symbol, args.config, pc.paths, recent_limit=None,
                                     uptime_sec=int(time.monotonic() - t_start),
                                     spot=spot.snapshot()),
            args.config, args.live, args.web_port, args.web_host, pc, lock,
            token=token,
            set_mode_fn=apply_mode,
        )
        url = f"http://127.0.0.1:{args.web_port}"
        console.print(f"[bold green]Web 控制台: {url}[/]")
        if args.web_host not in ("127.0.0.1", "localhost"):
            import socket

            try:
                ip = socket.gethostbyname(socket.gethostname())
                console.print(f"[bold green]局域网访问: http://{ip}:{args.web_port}?token={token}[/]")
            except OSError:
                pass
        if args.web_only:
            try:
                webbrowser.open(url)  # 本机回环访问免 token（局域网访问需带 token，见启动日志）
            except Exception:
                pass
            console.print("[dim]终端面板已隐藏；可在 Web 控制台「终端面板」按钮打开[/]")
    try:
        with Live(console=console, refresh_per_second=1 / args.refresh, screen=False) as live:
            while True:
                if pc.show_tui:
                    try:
                        v = _build_live_view(args.symbol, args.config, pc.paths,
                                             uptime_sec=int(time.monotonic() - t_start))
                        if args.web_port > 0:
                            v.loop_alive = pc.loop_alive()
                        live.update(render(v))
                    except Exception as e:  # 只读面板：任何异常都不崩溃，显示错误继续
                        live.update(f"读取失败: {e}")
                time.sleep(args.refresh)
    except KeyboardInterrupt:
        console.print("\n[dim]监控退出[/]")
    finally:
        if server is not None:
            server.shutdown()
        proc = pc.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
