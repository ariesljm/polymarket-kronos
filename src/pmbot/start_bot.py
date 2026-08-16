"""一键启动：主循环（后台）+ 监控面板（前台）。

用法:
  uv run python -m pmbot.start_bot --dry-run    # 模拟（默认）
  uv run python -m pmbot.start_bot --live       # 实盘（真钱！）
  start.bat                                     # Windows 双击（等价于 --dry-run）

Ctrl-C 退出面板时自动停掉主循环子进程。
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

LOG_FILE = "logs/bot.log"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键启动：主循环 + 监控面板")
    parser.add_argument("--live", action="store_true", help="实盘模式（默认 dry-run）")
    parser.add_argument("--dry-run", dest="live", action="store_false", help="模拟模式（默认）")
    parser.set_defaults(live=False)
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args(argv)

    mode = "--live" if args.live else "--dry-run"
    # 实盘与 dry-run 数据目录分离：交易历史/统计互不污染（dry-run 用 data/，实盘用 data_live/）
    from pmbot.paths import paths_for

    data_dir = paths_for(args.live).data_dir

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    from pmbot.single_instance import run_with_guard

    return run_with_guard("start_bot", lambda: _main_with_guard(args, mode, data_dir),
                          pid_file=f"{data_dir}/bot.pids")


def _main_with_guard(args, mode: str, data_dir: str) -> int:
    import pmbot.monitor as monitor

    # 主循环子进程：输出进 logs/bot.log（不占终端）
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        bot = subprocess.Popen(
            [sys.executable, "-m", "pmbot.run", mode, "--config", args.config,
             "--data-dir", data_dir],
            stdout=f, stderr=subprocess.STDOUT,
        )
    logging.info("主循环已启动（PID %s, %s, data=%s）", bot.pid, mode, data_dir)

    try:
        # 前台跑监控面板（web-only：不渲染终端面板，浏览器打开 Web 控制台；
        # 传入运行模式/配置/数据目录，供 Web 控制台启动主循环时保持一致）；
        # Ctrl-C 退出时顺带停主循环
        monitor.main([mode, "--config", args.config, "--web-only", "--data-dir", data_dir])
    except KeyboardInterrupt:
        pass
    finally:
        bot.terminate()
        try:
            bot.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bot.kill()
        logging.info("主循环已停止（PID %s）", bot.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
