"""运行路径与协调状态：数据目录派生的单一事实源。

模拟/实盘两套数据的路径（status/trades/log_dir/pid_file）统一由
RuntimePaths 派生；monitor 与 web 控制台共享 ProcessControl 协调状态，
替代裸 holder dict（魔法键）与 args 多属性手工同步。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


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
    """monitor ↔ web 控制台共享的进程协调状态（替代 holder dict）。"""

    proc: subprocess.Popen | None = None
    show_tui: bool = True
    live: bool = False
    paths: RuntimePaths = field(default_factory=RuntimePaths)
