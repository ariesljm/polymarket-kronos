"""文件原子写工具：临时文件 + replace（跨模块共用）。

防半写/崩溃损坏：先写 tmp 再原子替换，任何时刻读到的是完整旧版或完整新版。
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pandas as pd


def atomic_write_text(path: Path, text: str, retries: int = 3, delay: float = 0.15) -> None:
    """原子写文本：先写 <path><suffix>.tmp 后 replace（同盘重命名，无撕裂）。

    Windows 上目标被其他进程短暂占用（杀软/索引/读取方）时 os.replace 会抛
    PermissionError：小幅重试（递增等待）后仍失败再抛，调用方决定降级。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    last_err: OSError | None = None
    for attempt in range(retries):
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            return
        except OSError as e:
            last_err = e
            time.sleep(delay * (attempt + 1))
    raise last_err


def df_to_csv_text(df: pd.DataFrame, columns: list[str]) -> str:
    """DataFrame → CSV 文本（供 atomic_write_text 原子落盘）。"""
    buf = io.StringIO()
    df.to_csv(buf, index=False, columns=columns)
    return buf.getvalue()
