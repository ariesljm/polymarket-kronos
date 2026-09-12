"""multi_panel（多标的 TUI）交易记录的时间显示：本地时区。

回归：曾直接用 ISO 字符串切片（`ts[5:16]`）→ 交易历史全程显示 UTC，
与单标的 TUI（panel_view._fmt_ts）不一致。
"""

from __future__ import annotations

import importlib.util
import io
from datetime import datetime
from pathlib import Path

from rich.console import Console

from pmbot.ledger import load_records

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "multi_panel.py"
_SPEC = importlib.util.spec_from_file_location("multi_panel_under_test", _SCRIPT)
multi_panel = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(multi_panel)

_TS = "2026-09-10T17:38:52+00:00"


def _records(tmp_path: Path):
    (tmp_path / "trades.csv").write_text(
        "ts,window_start,symbol,direction,entry_price,exit_price,size,pnl,reason\n"
        f"{_TS},1789000000,ETH,down,0.5,1.0,2.0,1.0,take_profit\n",
        encoding="utf-8",
    )
    return load_records(tmp_path)


def _render(table) -> str:
    buf = io.StringIO()
    Console(file=buf, width=200, no_color=True).print(table)
    return buf.getvalue()


def test_history_table_ts_is_local_not_utc(tmp_path):
    """时间戳经本地时区转换，而非直接切 ISO 字符串（= UTC 串）。"""
    local = datetime.fromisoformat(_TS).astimezone(None).strftime("%m-%d %H:%M")
    utc_slice = "09-10 17:38"  # ts[5:16].replace("T", " ") 的旧行为
    rendered = _render(multi_panel._history_table({"eth": _records(tmp_path)}))
    assert local in rendered
    if local != utc_slice:  # 非 UTC 时区：必须与 UTC 串不同，才算真的转换过
        assert utc_slice not in rendered
