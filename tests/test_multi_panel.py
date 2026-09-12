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

import pytest

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


def test_deploy_line_live_shows_wallet_balance():
    """实盘底部汇总：显示钱包余额 + 今日盈亏（与卡片同口径），不再显示假投入。"""
    from pmbot.panel_view import PanelView

    views = [
        PanelView(symbol="ETH", balance=100.5, today_pnl=0.55),
        PanelView(symbol="BTC", balance=100.5, today_pnl=-0.11),
    ]
    rendered = _render(multi_panel._deploy_line(1.0, 2, {}, views, "live"))
    assert "$100.50" in rendered
    assert "今日" in rendered and "按交易" in rendered
    assert "投入" not in rendered  # 实盘不再用 amount×symbols 冒充投入


def test_deploy_line_dry_run_shows_invested():
    """dry-run 无真实余额（委托真实钱包查询，未配凭证为 None）→ 保持投入文案。"""
    from pmbot.panel_view import PanelView

    views = [PanelView(symbol="ETH"), PanelView(symbol="BTC")]
    rendered = _render(multi_panel._deploy_line(1.0, 6, {}, views, "dry-run"))
    assert "投入" in rendered and "$6" in rendered


def test_countdown_bar_color_scales_with_remaining():
    """倒计时颜色随剩余比例变：>50% 绿、20-50% 黄、<20% 红（接近禁入）。"""
    from pmbot.panel_view import PanelView

    # 5m 窗口，剩 240s（80%）→ 绿
    green = _render(multi_panel._countdown_bar(
        [PanelView(symbol="ETH", window_remaining_sec=240)], "5m"))
    assert "04:00" in green and "█" in green
    # 剩 90s（30%）→ 黄
    yellow = _render(multi_panel._countdown_bar(
        [PanelView(symbol="ETH", window_remaining_sec=90)], "5m"))
    assert "01:30" in yellow
    # 剩 30s（10%）→ 红（接近禁入）
    red = _render(multi_panel._countdown_bar(
        [PanelView(symbol="ETH", window_remaining_sec=30)], "5m"))
    assert "00:30" in red


def test_main_refreshes_in_loop_not_single_frame(monkeypatch):
    """回归：main 必须循环刷新（旧实现只渲染一帧就 return 0）。

    事故：删除底部状态栏时误删 `while True:`（缩进仍然合法，测试照常全绿）。
    后果是面板“闪一下”就退出，而退出会连带停掉 bot（启动 4 秒后优雅停机），
    且首帧显示的是上一轮残留 status → 看起来像 bot 离线/盘口全空。
    """
    renders = []

    class FakeLive:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def update(self, frame):
            renders.append(frame)
            if len(renders) >= 3:
                raise KeyboardInterrupt  # 结束无限循环（生产由 Ctrl-C 触发）

    monkeypatch.setattr(multi_panel, "Live", FakeLive)
    monkeypatch.setattr(multi_panel.time, "sleep", lambda _s: None)
    monkeypatch.setattr(multi_panel, "build_multi_view", lambda *a, **kw: [])
    monkeypatch.setattr(multi_panel, "load_records", lambda *a, **kw: [])
    monkeypatch.setattr(multi_panel, "console", Console(file=io.StringIO(), width=200))

    with pytest.raises(KeyboardInterrupt):
        multi_panel.main(["--live", "--data-dir", "data_live/btc"])

    assert len(renders) == 3  # 旧实现只有 1 帧


def test_card_prefers_spot_snapshot_over_strategy_quantized():
    """卡片现货价优先用 status["spot"] 全精度价，而非反解文案的 0.001% 量化台阶。"""
    from pmbot.panel_view import PanelView

    v = PanelView(
        symbol="BTC",
        strategy_state="策略状态: momentum 基准 77,200.01 偏离 +0.032%",
        spot={"price": 77_224.65, "delta": 3.2, "age": 0.4})
    rendered = _render(multi_panel._card(v, True, None))
    assert "$77,224.65" in rendered      # spot 全精度价
    assert "$77,224.71" not in rendered  # 反解量化价（基准×(1+0.032%)≈77,224.71）
    assert "▲0.03%" in rendered         # 窗口偏离% 仍来自策略文案
    assert "⚠币安" not in rendered       # age 0.4s 新鲜，不提示


def test_card_shows_ticker_staleness_warning():
    """币安 feed 静默在卡片上可见，不靠肉眼看价格僵死。

    阈值：>5s 黄（稀疏标的正常推送间隔 2-5s，2s 阈值会频繁误报）、≥10s 红。
    """
    from pmbot.panel_view import PanelView

    # 稀疏标的正常间隔（2-5s）不提示，防误报刷屏
    quiet = PanelView(symbol="DOGE", spot={"price": 0.085, "delta": 0.0, "age": 3.5})
    assert "⚠币安" not in _render(multi_panel._card(quiet, True, None))
    # 超过 5s：黄色提示
    late = PanelView(symbol="BTC", spot={"price": 77_000.0, "delta": 0.0, "age": 6.7})
    assert "⚠币安7s前" in _render(multi_panel._card(late, True, None))
    # 超过 10s：红色（真卡死，REST 兜底已接管 5s）
    stuck = PanelView(symbol="BTC", spot={"price": 77_000.0, "delta": 0.0, "age": 12.0})
    assert "⚠币安12s前" in _render(multi_panel._card(stuck, True, None))


def test_card_falls_back_to_strategy_price_without_spot():
    """无 status.spot（旧版本 bot 数据）→ 回落反解策略文案价。"""
    from pmbot.panel_view import PanelView

    v = PanelView(symbol="BTC",
                  strategy_state="策略状态: momentum 基准 77,200.01 偏离 +0.032%")
    rendered = _render(multi_panel._card(v, True, None))
    assert "$77,224.71" in rendered
