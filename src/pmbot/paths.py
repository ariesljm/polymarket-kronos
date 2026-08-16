"""运行路径与协调状态：数据目录派生的单一事实源。

模拟/实盘两套数据的路径（status/trades/log_dir/pid_file）统一由
RuntimePaths 派生；monitor 与 web 控制台共享 ProcessControl 协调状态，
替代裸 holder dict（魔法键）与 args 多属性手工同步。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pmbot.single_instance import InstanceGuard


def spawn_loop(config: str, live: bool, data_dir: str = "data") -> subprocess.Popen:
    """拉起主循环子进程（输出进 logs/bot.log，与 start_bot 同一路径）。

    venv 下用 base python + 手动 PYTHONPATH 直启：绕开 .venv shim 的
    Job Object 联动（shim 被杀/退出会连带强杀本进程与子进程，导致 run
    残留孤儿——见 start_bot watchdog 注释）。单实例守护由 run.py 自身
    （run_with_guard("run")）负责。Web 控制台 start 按钮与 start_bot
    桌面双击共用本实现，启动参数拼装不再分裂。
    """
    mode = "--live" if live else "--dry-run"
    os.makedirs("logs", exist_ok=True)
    run_env = None
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        run_env = os.environ.copy()
        site = str(Path(venv) / "Lib" / "site-packages")
        src = str(Path(__file__).resolve().parent.parent)  # src/（pmbot 包所在）
        run_env["PYTHONPATH"] = site + os.pathsep + src + os.pathsep + run_env.get("PYTHONPATH", "")
        run_py = getattr(sys, "_base_executable", None) or sys.executable
    else:
        run_py = sys.executable  # 无 venv（裸环境）：原样启动
    logf = open(Path("logs") / "bot.log", "a", encoding="utf-8")
    return subprocess.Popen(
        [run_py, "-m", "pmbot.run", mode, "--config", config,
         "--data-dir", data_dir],
        env=run_env, stdout=logf, stderr=subprocess.STDOUT,
    )


@dataclass(frozen=True)
class RuntimePaths:
    """运行时数据路径集：显式未指定时从 data_dir 派生。"""

    data_dir: str = "data"
    mode: str = "dry-run"  # "dry-run" / "live"
    status: str | None = None
    trades: str | None = None
    log_dir: str | None = None
    pid_file: str | None = None

    def __post_init__(self) -> None:
        derived = {
            "status": f"{self.data_dir}/status.json",
            "trades": f"{self.data_dir}/trades.csv",
            "log_dir": self.data_dir,
            "pid_file": f"{self.data_dir}/bot.pids",
        }
        for fname in ("status", "trades", "log_dir", "pid_file"):
            if getattr(self, fname) is None:
                object.__setattr__(self, fname, derived[fname])

    @property
    def live(self) -> bool:
        return self.mode == "live"


def paths_for(live: bool, data_dir: str | None = None) -> RuntimePaths:
    """按运行模式派生路径：实盘默认 data_live/，模拟默认 data/（可显式覆盖）。"""
    dd = data_dir or ("data_live" if live else "data")
    return RuntimePaths(data_dir=dd, mode="live" if live else "dry-run")


@dataclass
class ProcessControl:
    """monitor ↔ web 控制台共享的进程协调状态（替代 holder dict）。

    除协调状态外，进程级操作（拉起/存活）也收敛于此：
    spawn 用当前模式与路径拉起主循环，loop_alive 查当前路径的 pid 文件。
    """

    proc: subprocess.Popen | None = None
    show_tui: bool = True
    live: bool = False
    paths: RuntimePaths = field(default_factory=RuntimePaths)

    def spawn(self, config: str) -> subprocess.Popen:
        """按当前模式/数据目录拉起主循环子进程。"""
        self.proc = spawn_loop(config, self.live, self.paths.data_dir)
        return self.proc

    def loop_alive(self) -> bool:
        """主循环（run 角色）进程是否存活（读当前 pid 文件 + 跨平台存活检查）。"""
        try:
            data = json.loads(Path(self.paths.pid_file).read_text(encoding="utf-8"))
            pid = data.get("run")
            if not pid:
                return False
            return InstanceGuard.alive(int(pid))
        except Exception:
            return False
