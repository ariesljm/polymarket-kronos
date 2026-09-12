"""入口装配支撑 seam：run / run_multi / start_bot 三入口共享的启动装配。

收敛曾以文件为界的 4 组复制（日志装配 / SIGTERM / 实盘自检 / 整树杀）——
同一套装配以 3 个入口为界裂成多份，改一处其它遗忘即漂移。三入口只保留
各自的 argparse 与编排，装配细节全部委托本模块。系统内 dedupe 先例：
fileio 原子写、ledger 账本读面（ADR-0003 精神）。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType

LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
# 降噪：httpx/py_clob 每 tick 刷屏的请求日志提升到 WARNING（曾占满日志 90%+）
NOISY_LOGGERS = ("httpx", "py_clob_client_v2.http_helpers.helpers", "httpcore")
# Windows 子进程不弹黑窗（taskkill 用；0x08000000 = CREATE_NO_WINDOW）
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def setup_logging(log_dir: Path, filename: str) -> Path:
    """按天滚动文件日志（保留 14 天，UTF-8）+ 交互终端（stderr 为 TTY）时
    附加控制台输出。后台启动（nohup/start /b 重定向）只写文件——日志唯一来源。

    幂等：已有 root handler 时跳过（同一进程多次调用 main 的防御）。
    """
    root = logging.getLogger()
    if root.handlers:
        return log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(LOG_FMT)
    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / filename, when="midnight", backupCount=14, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    handlers: list[logging.Handler] = [fh]
    if sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        handlers.append(sh)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_dir


def on_sigterm() -> None:
    """终端关闭（CTRL_CLOSE/LOGOFF/SHUTDOWN）在 Windows 上触发 SIGTERM：
    转为 KeyboardInterrupt 走统一优雅退出路径（finally 整树清理）。"""

    def _on_sigterm(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass  # 非主线程/平台不支持时退化为 KeyboardInterrupt


def run_live_preflight(cfg) -> int:
    """实盘前置自检（真钱守门）：返回 0 = 通过，3 = 拒绝启动。

    先写回代理环境——py_clob_client 的 httpx 客户端在首次调用时才创建，
    环境变量必须在此之前就位（resolve_proxy 幂等，run.py 已先行调用无妨）。
    仅 live 入口调用；dry-run 不触碰真实凭证。
    """
    from pmbot.clob_executor import ClobExecutor
    from pmbot.preflight import live_advisories, live_preflight
    from pmbot.run import resolve_proxy

    resolve_proxy()
    problems = live_preflight(cfg, ClobExecutor())
    if problems:
        for p in problems:
            logging.error("实盘自检未通过：%s", p)
        logging.error("拒绝以实盘模式启动（修正后重试；先干跑请用 start_multi.bat）")
        return 3
    for note in live_advisories(cfg):
        logging.warning("实盘提醒：%s", note)
    logging.info("实盘自检通过：凭证 / 余额 / 授权 均正常")
    return 0


def kill_tree(pid: int) -> None:
    """整树强杀（uv shim → base python 双层结构）。

    terminate() 只能杀直接子进程（uv shim），base python 会成孤儿继续跑——
    start_bot 看门狗与 single_instance 杀旧共用同一策略（taskkill /T /F）。
    Windows 的 taskkill /T /F 已是强杀；其余平台先 SIGTERM（优雅退出由调用方
    决定是否等待，见 single_instance._kill）。
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass