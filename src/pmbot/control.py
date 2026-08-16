"""面板 ↔ 主循环控制通道：data/control.json 指令文件。

面板（monitor / Web 页面）只写指令；主循环每 tick 读取并消费（读后即删）。
原子写 + 读删，无锁无竞态；指令集：resume（恢复暂停）/ reset（清除数据）/
stop（优雅停止）。start 属于进程级操作，由面板直接拉起子进程，不走本通道。
"""

from __future__ import annotations

import json
from pathlib import Path

from pmbot.fileio import atomic_write_text

CONTROL_FILE = "data/control.json"
COMMANDS = ("resume", "reset", "stop")


def write_control(cmd: str, path: str | Path = CONTROL_FILE) -> None:
    """写入一条控制指令（原子写；面板侧调用）。"""
    if cmd not in COMMANDS:
        raise ValueError(f"未知控制指令: {cmd}，可用: {COMMANDS}")
    atomic_write_text(Path(path), json.dumps({"cmd": cmd}))


def read_control(path: str | Path = CONTROL_FILE) -> str | None:
    """读取并消费一条控制指令（读后即删）；无指令/损坏返回 None。"""
    p = Path(path)
    if not p.is_file():
        return None
    cmd = None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        c = data.get("cmd")
        if c in COMMANDS:
            cmd = c
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    finally:
        # 无论指令是否合法都删除：防止坏文件阻塞通道
        try:
            p.unlink()
        except OSError:
            pass
    return cmd


def reset_runtime(status_path: str | Path, trades_path: str | Path, data_dir: str | Path,
                  *, symbol: str | None = None, interval: str | None = None,
                  config: str | None = None) -> None:
    """清除运行数据文件（主循环与面板共用同一 reset 语义）。

    删除 status/trades/K线/预测记录；下次启动自动重建。
    symbol/interval 未显式给出时从 status.json / config 读取（面板侧路径）；
    主循环侧显式传入（symbol=self.symbol, interval=self.discovery.interval）。
    """
    symbol = symbol or _read_symbol(status_path)
    interval = interval or _read_interval(config)
    clear_data_files(status_path, trades_path, data_dir, symbol, interval)


def _read_symbol(status_path: str | Path) -> str:
    """从 status.json 读取交易标的（读取失败返回空串）。"""
    try:
        return str(json.loads(Path(status_path).read_text(encoding="utf-8")).get("symbol", ""))
    except Exception:
        return ""


def _read_interval(config: str | None) -> str:
    """从配置文件读取 market_interval（读取失败/未提供返回默认 15m）。"""
    if config is None:
        return "15m"
    try:
        from pmbot.config import load_config

        return load_config(config).market_interval
    except Exception:
        return "15m"


def clear_data_files(status_path: str | Path, trades_path: str | Path, data_dir: str | Path,
                     symbol: str, interval: str) -> None:
    """清除交易运行数据文件（主循环未运行时由面板直接调用）。

    删除 status/trades/K 线/预测记录；下次启动自动重建。symbol/interval
    由调用方从 status.json 与 config 读取（本函数保持纯文件操作）。
    """
    targets = [
        Path(status_path),
        Path(trades_path),
        Path(data_dir) / f"{symbol.lower()}_{interval}.csv",
        Path(data_dir) / f"predictions_{symbol.lower()}.csv",
    ]
    for p in targets:
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass
