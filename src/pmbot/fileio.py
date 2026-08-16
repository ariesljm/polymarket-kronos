"""文件原子写工具：临时文件 + replace（跨模块共用）。

防半写/崩溃损坏：先写 tmp 再原子替换，任何时刻读到的是完整旧版或完整新版。
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd


def atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：写 <path><suffix>.tmp 后 replace（同盘重命名，无撕裂）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def df_to_csv_text(df: pd.DataFrame, columns: list[str]) -> str:
    """DataFrame → CSV 文本（供 atomic_write_text 原子落盘）。"""
    buf = io.StringIO()
    df.to_csv(buf, index=False, columns=columns)
    return buf.getvalue()
