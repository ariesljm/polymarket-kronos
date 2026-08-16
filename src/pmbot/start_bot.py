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

LOG_FILE = "data/bot.log"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键启动：主循环 + 监控面板")
    parser.add_argument("--live", action="store_true", help="实盘模式（默认 dry-run）")
    parser.add_argument("--dry-run", dest="live", action="store_false", help="模拟模式（默认）")
    parser.set_defaults(live=False)
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args(argv)

    mode = "--live" if args.live else "--dry-run"

    os.makedirs("data", exist_ok=True)

    from pmbot.single_instance import run_with_guard

    return run_with_guard("start_bot", lambda: _main_with_guard(args, mode))


def _main_with_guard(args, mode: str) -> int:
    import pmbot.monitor as monitor

    # 主循环子进程：输出进 data/bot.log（不占终端）
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        bot = subprocess.Popen(
            [sys.executable, "-m", "pmbot.run", mode, "--config", args.config],
            stdout=f, stderr=subprocess.STDOUT,
        )
    logging.info("主循环已启动（PID %s, %s）", bot.pid, mode)

    try:
        # 前台跑监控面板（参数已被本入口消费，传空 argv）；Ctrl-C 退出时顺带停主循环
        monitor.main([])
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
