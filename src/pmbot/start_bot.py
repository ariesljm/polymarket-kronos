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
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _kill_tree(pid: int) -> None:
    """整树强杀子进程（uv shim → base python 双层结构）。

    terminate() 只能杀直接子进程（uv shim），base python 会成孤儿继续跑——
    与 single_instance._kill 同一策略（taskkill /T /F）。
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


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

    # 终端关闭（CTRL_CLOSE/LOGOFF/SHUTDOWN）在 Windows 上触发 SIGTERM：
    # 转为 KeyboardInterrupt 走统一优雅退出路径（finally 整树清理）。
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass

    # 主循环子进程：输出进 logs/bot.log（不占终端）。
    # spawn 细节（base python + PYTHONPATH 绕 venv shim、日志重定向）收敛在
    # paths.spawn_loop——Web 控制台 start 按钮与桌面双击共用同一实现。
    from pmbot.paths import spawn_loop
    from pmbot.single_instance import InstanceGuard

    bot = spawn_loop(args.config, args.live, data_dir)
    logging.info("主循环已启动（PID %s, %s, data=%s）", bot.pid, mode, data_dir)

    # 父进程看门狗：uv run 的 shim 层脱离控制台信号，关闭终端窗口时
    # 信号传不到本进程——改为主动探测终端进程（父进程）存活，
    # 父进程退出（终端关闭）即整树清理子进程并自退出。
    parent_pid = os.getppid() if hasattr(os, "getppid") else None
    _wd_log = open("logs/watchdog.log", "a", encoding="utf-8")

    def _watch_parent() -> None:
        if not parent_pid:
            _wd_log.write(f"[{time.time():.0f}] 无父进程可监视\n")
            _wd_log.flush()
            return
        _wd_log.write(f"[{time.time():.0f}] 监视父进程 PID={parent_pid}\n")
        _wd_log.flush()
        while True:
            time.sleep(2)
            # 存活检查用 ctypes OpenProcess（PROCESS_QUERY_LIMITED_INFORMATION）：
            # Windows 上 os.kill(pid, 0) 是 TerminateProcess(pid, 0) 而非存在性检查，
            # 对双击 bat 启动的 cmd 父进程稳定失败（OSError 22）→ 误判父进程退出、
            # 误杀主循环并自杀（watchdog.log "父进程已退出"）。
            if not InstanceGuard.alive(parent_pid):
                _wd_log.write(f"[{time.time():.0f}] 父进程已退出 poll={bot.poll()} pid={bot.pid}\n")
                _wd_log.flush()
                try:
                    if bot.poll() is None:
                        # 只看 returncode，不读输出：taskkill 在中文系统输出 GBK，
                        # text=True 按 UTF-8 解码会抛 UnicodeDecodeError（reader 线程崩溃）
                        r = subprocess.run(
                            ["taskkill", "/PID", str(bot.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        _wd_log.write(f"[{time.time():.0f}] taskkill rc={r.returncode}\n")
                        _wd_log.flush()
                except Exception as e:
                    _wd_log.write(f"[{time.time():.0f}] watchdog 清理异常: {e}\n")
                    _wd_log.flush()
                _wd_log.write(f"[{time.time():.0f}] watchdog 退出\n")
                _wd_log.flush()
                os._exit(0)

    threading.Thread(target=_watch_parent, daemon=True).start()

    try:
        # 前台跑监控面板（web-only：不渲染终端面板，浏览器打开 Web 控制台；
        # 传入运行模式/配置/数据目录，供 Web 控制台启动主循环时保持一致）；
        # Ctrl-C 退出时顺带停主循环
        monitor.main([mode, "--config", args.config, "--web-only", "--data-dir", data_dir])
    except KeyboardInterrupt:
        pass
    finally:
        _kill_tree(bot.pid)  # 整树杀（uv shim + base python 一并清理）
        try:
            bot.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        logging.info("主循环已停止（PID %s）", bot.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
