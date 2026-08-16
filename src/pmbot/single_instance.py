"""单实例守护：启动时清理同角色旧进程，防止多实例互踩状态文件。

PID 文件 data/bot.pids 记录各角色 pid（start_bot / run），JSON 原子写。
新实例启动时先杀掉旧实例再注册自己；正常退出时注销。

Windows 注意：os.kill(pid, 0) 存在假阳性（进程已死仍报存活），
存活检查用 ctypes OpenProcess；杀进程用 taskkill /T（整树，覆盖 uv shim→base 两层）。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from pmbot.fileio import atomic_write_text

logger = logging.getLogger(__name__)

PID_FILE = "data/bot.pids"

_CREATE_NO_WINDOW = 0x08000000
_OPEN_PROCESS_QUERY_LIMITED = 0x1000


class InstanceGuard:
    """按角色注册/清理的单实例锁（不跨进程加锁，靠"新杀旧"保证唯一）。"""

    def __init__(self, pid_file: str | Path = PID_FILE):
        self.path = Path(pid_file)

    # ---- 内部 ----

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(data))

    @staticmethod
    def alive(pid: int) -> bool:
        if sys.platform == "win32":
            import ctypes

            h = ctypes.windll.kernel32.OpenProcess(_OPEN_PROCESS_QUERY_LIMITED, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @classmethod
    def _kill(cls, pid: int) -> None:
        if sys.platform == "win32":
            # taskkill /T：整树杀（uv shim 派生 base python 的两层结构一并清理）
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
            )
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        for _ in range(6):  # 最多等 3 秒优雅退出
            if not cls.alive(pid):
                return
            time.sleep(0.5)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    # ---- 对外接口 ----

    def kill_old(self, name: str) -> None:
        """杀掉 pid 文件中该角色记录的旧进程（不存在/已死则跳过）。"""
        pid = self._read().get(name)
        if pid and self.alive(pid):
            logger.warning("检测到旧 %s 实例（PID %d），正在终止", name, pid)
            self._kill(pid)

    def register(self, name: str, pid: int | None = None) -> None:
        """记录本实例 pid（默认当前进程；start_bot 记录子进程主循环时传 pid）。"""
        data = self._read()
        data[name] = pid or os.getpid()
        self._write(data)

    def unregister(self, name: str) -> None:
        """注销本实例（仅当记录的就是自己时才删除）。"""
        data = self._read()
        if data.get(name) == os.getpid():
            data.pop(name, None)
            self._write(data)


def run_with_guard(role: str, fn, pid_file: str | Path = PID_FILE):
    """按角色执行受单实例守护包裹的业务函数。

    新实例先杀旧实例（同角色）再注册自己；fn 结束（含异常）自动注销。
    start_bot / run 两个入口共用，防止多实例互踩状态文件。
    """
    guard = InstanceGuard(pid_file)
    guard.kill_old(role)
    guard.register(role)
    try:
        return fn()
    finally:
        guard.unregister(role)
