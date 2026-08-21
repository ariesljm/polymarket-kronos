"""父进程看门狗：终端关闭时整树清理子进程。

start_bot 的关注点之一：uv run 的 shim 层脱离控制台信号，关闭终端窗口时
信号传不到本进程——主动探测父进程（终端）存活，父进程退出即整树清理
子进程并自退出。提取为独立模块使其可测（注入存活检查与清理回调）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


class ParentWatchdog:
    """后台守护线程：探测父进程存活，父进程退出即清理子进程并自退出。

    依赖经窄接口注入：
    - is_alive(pid) → bool：跨平台存活检查（Windows 用 OpenProcess）
    - on_parent_exit(pid)：父进程退出时的清理回调（通常整树强杀子进程）
    """

    def __init__(
        self,
        *,
        parent_pid: int | None,
        child_process,
        is_alive: Callable[[int], bool],
        on_parent_exit: Callable[[int], None],
        poll_interval: float = 2.0,
    ):
        self._parent_pid = parent_pid
        self._child = child_process
        self._is_alive = is_alive
        self._on_parent_exit = on_parent_exit
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._log = open("logs/watchdog.log", "a", encoding="utf-8")

    def start(self) -> None:
        """启动后台探测线程（daemon）。"""
        if self._thread is not None:
            return
        if not self._parent_pid:
            self._log.write(f"[{time.time():.0f}] 无父进程可监视\n")
            self._log.flush()
            return
        self._log.write(f"[{time.time():.0f}] 监视父进程 PID={self._parent_pid}\n")
        self._log.flush()
        self._thread = threading.Thread(target=self._run, name="parent-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            if not self._is_alive(self._parent_pid):
                self._log.write(
                    f"[{time.time():.0f}] 父进程已退出 poll={self._child.poll()} "
                    f"pid={self._child.pid}\n"
                )
                self._log.flush()
                try:
                    if self._child.poll() is None:
                        self._on_parent_exit(self._child.pid)
                        self._log.write(
                            f"[{time.time():.0f}] on_parent_exit 调用完成\n"
                        )
                except Exception as e:
                    self._log.write(f"[{time.time():.0f}] watchdog 清理异常: {e}\n")
                self._log.write(f"[{time.time():.0f}] watchdog 退出\n")
                self._log.flush()
                os._exit(0)
