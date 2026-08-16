"""监控面板视图构建测试：纯函数 build_view + 渲染冒烟。"""

import json
import time

import pytest

from datetime import datetime, timedelta, timezone

from pmbot.monitor import PanelConfig, SpotPrice, build_view
from pmbot.state import Position, TradeState
from pmbot.types import Direction, PendingOrder, Signal

WINDOW_START = int(datetime(2026, 8, 14, 3, 30, tzinfo=timezone.utc).timestamp())


def _as_signal(s):
    if s is None or isinstance(s, Signal):
        return s
    return Signal(direction=Direction(s["direction"]), p_up=s.get("p_up"))


def _as_pending(p):
    if p is None or isinstance(p, PendingOrder):
        return p
    return PendingOrder(
        direction=Direction(p["direction"]), price=p["price"], size=p.get("size", 0.0),
        order_id=p.get("order_id", ""), created_sec=p.get("created_sec", 0),
    )


def _as_position(p):
    if p is None or isinstance(p, Position):
        return p
    return Position(
        direction=Direction(p["direction"]), entry_price=p["entry_price"], size=p["size"],
        entered_remaining_sec=p.get("entered_remaining_sec", 0), window_start=p.get("window_start", 0),
    )


def status_dict(**over):
    data = {
        "symbol": "BTC",
        "window_start": WINDOW_START,
        "window_bet_placed": True,
        "signal": _as_signal({"direction": "up", "p_up": 0.63}),
        "pending_order": _as_pending({"direction": "up", "price": 0.45, "size": 5.0, "order_id": "oid-1", "created_sec": 1}),
        "position": _as_position({"direction": "up", "entry_price": 0.45, "size": 5.0,
                                  "entered_remaining_sec": 300, "window_start": WINDOW_START}),
        "consecutive_losses": 2,
        "daily_loss": 4.5,
        "paused": False,
        "was_paused": False,
        "last_day": "2026-08-14",
        "last_predict_sec": WINDOW_START + 30,
    }
    for k in ("signal", "pending_order", "position"):
        if k in over:
            conv = {"signal": _as_signal, "pending_order": _as_pending, "position": _as_position}[k]
            data[k] = conv(over.pop(k))
    data.update(over)
    return TradeState(**data)


def test_today_pnl_uses_balance_diff_when_available():
    """实盘今日盈亏 = 现余额 − 今日起始基准（余额差口径，覆盖交易聚合）。"""
    from pmbot.monitor import render

    rows = trades_rows(1, pnl=0.55)  # 交易聚合 +0.55，但余额差 +5.00 优先
    v = build_view(status_dict(balance=25.0, day_start_balance=20.0), rows, None,
                   now_sec=WINDOW_START, today=TODAY)
    assert v.today_pnl == pytest.approx(5.0)
    assert v.today_pnl_src == "balance"
    text = render(v)
    assert "今日盈亏: +5.00 USDC（余额差）" in text


def test_today_pnl_falls_back_to_trades_aggregate():
    """无余额基准（dry-run/未捕获）时回退交易聚合口径。"""
    v = build_view(status_dict(), trades_rows(2, pnl=0.55), None,
                   now_sec=WINDOW_START, today=TODAY)
    assert v.today_pnl == pytest.approx(1.10)
    assert v.today_pnl_src == ""


def trades_rows(n=3, pnl=0.55):
    base = datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)
    return [
        {"ts": (base + timedelta(minutes=i * 15)).isoformat(), "window_start": WINDOW_START,
         "symbol": "BTC", "direction": "up", "entry_price": 0.45, "exit_price": 0.56,
         "size": 5.0, "pnl": pnl, "reason": "settle"}
        for i in range(n)
    ]


TODAY = "2026-08-14"


def test_view_extracts_status_fields():
    v = build_view(status_dict(), trades_rows(), {"correct": 35, "total": 60, "accuracy": 0.583},
                   now_sec=WINDOW_START + 60, today=TODAY, local_tz=timezone.utc, panel=PanelConfig(model_variant="kronos-small"))
    assert v.symbol == "BTC"
    assert v.window_label == "08-14 03:30-03:45"
    assert v.window_remaining_sec == 900 - 60
    assert v.signal == {"direction": "up", "p_up": 0.63}
    assert v.pending == {"direction": "up", "price": 0.45, "size": 5.0}
    assert v.position == {"direction": "up", "entry_price": 0.45, "size": 5.0}
    assert v.paused is False
    assert v.consecutive_losses == 2
    assert v.daily_loss == 4.5
    assert v.last_predict_sec == WINDOW_START + 30
    assert v.model_variant == "kronos-small"  # 注入


def test_view_missing_status_safe():
    v = build_view(None, trades_rows(), None, now_sec=WINDOW_START)
    assert v.symbol == "—"
    assert v.window_label == "—"
    assert v.window_remaining_sec is None
    assert v.signal is None
    assert v.position is None
    assert v.paused is False
    assert v.consecutive_losses == 0


def test_view_partial_status_no_position():
    st = status_dict(position=None, pending_order=None, signal=None)
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START)
    assert v.position is None
    assert v.pending is None
    assert v.signal is None


def test_today_stats_and_recent_trades():
    rows = trades_rows(n=4, pnl=0.55) + trades_rows(n=1, pnl=-2.25)
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START, today=TODAY)  # noqa: E501
    # 4×0.55 - 2.25 = -0.05
    assert v.today_pnl == -0.05
    assert v.today_trades == 5
    assert len(v.recent_trades) == 5  # 截断 5 条


def test_recent_trades_truncated():
    rows = trades_rows(n=8)
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START)
    assert len(v.recent_trades) == 5
    assert v.recent_trades[0]["pnl"] == 0.55


def test_accuracy_passthrough():
    acc = {"correct": 35, "total": 60, "accuracy": 0.583}
    v = build_view(status_dict(), trades_rows(), acc, now_sec=WINDOW_START)
    assert v.accuracy == acc


def test_paused_flag():
    v = build_view(status_dict(paused=True), trades_rows(), None, now_sec=WINDOW_START)
    assert v.paused is True
    assert v.pause_reason is None


def test_pause_reason_shown():
    """暂停时显示熔断原因。"""
    from pmbot.monitor import render

    st = status_dict(paused=True, pause_reason="日亏 10.30 USDC（上限 10）")
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY)
    text = render(v)
    assert "暂停: ⚠️ 是（日亏 10.30 USDC（上限 10））" in text
    # 未暂停不显示原因
    v = build_view(status_dict(paused=False), trades_rows(), None, now_sec=WINDOW_START, today=TODAY)
    assert "暂停: 否" in render(v)


def test_render_smoke():
    from pmbot.monitor import render

    v = build_view(status_dict(), trades_rows(), {"correct": 35, "total": 60, "accuracy": 0.583},
                   now_sec=WINDOW_START + 60, local_tz=timezone.utc, panel=PanelConfig(model_variant="kronos-small"))
    text = render(v)
    assert "PMBOT" in text
    assert "BTC" in text
    assert "kronos-small" in text
    assert "已推理" in text
    assert "08-14 03:30-03:45" in text
    assert "@45" in text
    assert "35/60" in text


def test_bad_rows_skipped():
    """坏行（缺字段）不影响视图，只跳过。"""
    rows = trades_rows(n=2) + [{"ts": None, "direction": "up", "pnl": "x"}, {"bad": "row"}]
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START, today=TODAY)
    assert v.today_trades == 2  # 坏行被跳过
    assert len(v.recent_trades) == 2


def test_signal_note_thresholds():
    """信号状态标注：达阈值/未达阈值。"""
    th = {"p_up_buy": 0.60, "p_down_buy": 0.40}
    # 中间地带 → 跳过
    v = build_view(status_dict(signal={"direction": "up", "p_up": 0.55}), trades_rows(), None,
                   now_sec=WINDOW_START, today=TODAY, panel=PanelConfig(model_variant="kronos-small", thresholds=th))
    assert v.signal_note == "未达阈值，跳过"
    # 高信心看涨 → 买入方向：up
    st = status_dict(signal={"direction": "up", "p_up": 0.63})
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY, panel=PanelConfig(thresholds=th))
    assert v.signal_note == "买入方向：up"
    # 高信心看跌 → 买入方向：down
    st = status_dict(signal={"direction": "down", "p_up": 0.35})
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY, panel=PanelConfig(thresholds=th))
    assert v.signal_note == "买入方向：down"


def test_render_shows_signal_note():
    from pmbot.monitor import render

    v = build_view(status_dict(signal={"direction": "up", "p_up": 0.55}), trades_rows(), None,
                   now_sec=WINDOW_START, today=TODAY, panel=PanelConfig(thresholds={"p_up_buy": 0.60, "p_down_buy": 0.40}))
    text = render(v)
    assert "未达阈值，跳过" in text


def test_inferred_yes_with_signal_no_last_predict():
    """旧 status（无 last_predict_sec）但有信号 → 判定已推理。"""
    from pmbot.monitor import render

    st = status_dict(last_predict_sec=None)
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY)
    text = render(v)
    assert "已推理" in text


def test_waiting_for_pullback_note():
    """信号候选但未挂单 → 等回调状态（盘口未到限价）。"""
    from pmbot.monitor import render

    # 候选 + 无挂单（挂单已撤）→ 本窗口未成交（trades 无本窗口平仓）
    v = build_view(status_dict(pending_order=None, position=None), [], None,
                   now_sec=WINDOW_START, today=TODAY,
                   panel=PanelConfig(thresholds={"p_up_buy": 0.60, "p_down_buy": 0.40}))
    assert v.status_note == "挂单已撤（窗口未成交）"
    # 候选 + 有挂单 → 挂单中
    v = build_view(status_dict(position=None), trades_rows(), None,
                   now_sec=WINDOW_START, today=TODAY,
                   panel=PanelConfig(thresholds={"p_up_buy": 0.60, "p_down_buy": 0.40}))
    assert v.status_note == "挂单中（等回调）"
    # 持仓 → 持仓中
    v = build_view(status_dict(pending_order=None, position={"direction": "up", "entry_price": 0.37, "size": 5.0,
                  "entered_remaining_sec": 300, "window_start": WINDOW_START}), trades_rows(), None,
                   now_sec=WINDOW_START, today=TODAY,
                   panel=PanelConfig(thresholds={"p_up_buy": 0.60, "p_down_buy": 0.40}))
    assert v.status_note == "持仓中"
    # 未达阈值 → 观望
    v = build_view(status_dict(signal={"direction": "up", "p_up": 0.55}, position=None, pending_order=None),
                   [], None, now_sec=WINDOW_START, today=TODAY,
                   panel=PanelConfig(thresholds={"p_up_buy": 0.60, "p_down_buy": 0.40}))
    assert v.status_note == "观望（未达阈值）"


def test_status_note_shows_actual_exit_reason():
    """本窗口已平仓：按 trades.csv reason 显示实际离场原因。"""
    th = {"p_up_buy": 0.60, "p_down_buy": 0.40}
    for reason, label in (("take_profit", "已止盈"), ("stop_loss", "已止损"),
                          ("time_stop", "已时间止损"), ("window_end", "窗口结束平仓"),
                          ("settle", "已结算")):
        rows = trades_rows(n=1)
        rows[-1]["reason"] = reason
        v = build_view(status_dict(pending_order=None, position=None), rows, None,
                       now_sec=WINDOW_START, today=TODAY, panel=PanelConfig(thresholds=th))
        assert v.status_note == label, reason


def test_status_note_other_window_trade_does_not_apply():
    """旧窗口的平仓不影响本窗口状态（仍显示挂单已撤/观望）。"""
    rows = trades_rows(n=1)
    rows[-1]["window_start"] = WINDOW_START - 300  # 上一窗口
    v = build_view(status_dict(pending_order=None, position=None), rows, None,
                   now_sec=WINDOW_START, today=TODAY,
                   panel=PanelConfig(thresholds={"p_up_buy": 0.60, "p_down_buy": 0.40}))
    assert v.status_note == "挂单已撤（窗口未成交）"


def test_view_market_prices():
    from pmbot.monitor import render

    st = status_dict(market_prices={"up_ask": 0.16, "up_bid": 0.15, "down_ask": 0.86, "down_bid": 0.85})
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY)
    assert v.prices["up_ask"] == 0.16
    text = render(v)
    assert "盘口 UP: 15/16" in text
    assert "DOWN: 85/86" in text


def test_view_market_prices_missing_safe():
    v = build_view(status_dict(), trades_rows(), None, now_sec=WINDOW_START, today=TODAY)
    assert v.prices is None


def test_position_line_shows_mark_and_floating_pnl():
    from pmbot.monitor import render

    st = status_dict(
        position={"direction": "up", "entry_price": 0.45, "size": 5.0,
                  "entered_remaining_sec": 300, "window_start": WINDOW_START},
        market_prices={"up_ask": 0.48, "up_bid": 0.47, "down_ask": 0.54, "down_bid": 0.53},
    )
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY)
    text = render(v)
    assert "持仓: UP 5.00股 @45" in text
    assert "现价 47" in text
    assert "浮动 +0.10" in text  # (0.47-0.45)×5


def test_position_line_without_mark_safe():
    from pmbot.monitor import render

    st = status_dict(
        position={"direction": "down", "entry_price": 0.36, "size": 5.0,
                  "entered_remaining_sec": 300, "window_start": WINDOW_START},
    )  # 无 market_prices
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY)
    text = render(v)
    assert "持仓: DOWN 5.00股 @36" in text
    assert "现价" not in text


def test_position_shows_tp_sl_prices():
    """持仓行显示动态止盈/止损价（美分）：entry 0.50 → 止盈 65 / 止损 40。"""
    from pmbot.monitor import render

    st = status_dict(
        position={"direction": "up", "entry_price": 0.50, "size": 5.0,
                  "entered_remaining_sec": 300, "window_start": WINDOW_START},
    )
    tp_sl = {"pct": 0.30, "max": 0.95, "sl": 0.20}
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY, panel=PanelConfig(tp_sl=tp_sl))
    text = render(v)
    assert "止盈 65" in text  # min(0.50×1.3, 0.95)
    assert "止损 40" in text  # 0.50×0.8


def test_position_tp_capped_by_max():
    """入场价高时止盈价按 take_profit_max 封顶：entry 0.80 → 止盈 95。"""
    from pmbot.monitor import render

    st = status_dict(
        position={"direction": "up", "entry_price": 0.80, "size": 2.0,
                  "entered_remaining_sec": 300, "window_start": WINDOW_START},
    )
    tp_sl = {"pct": 0.30, "max": 0.95, "sl": 0.20}
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY, panel=PanelConfig(tp_sl=tp_sl))
    text = render(v)
    assert "止盈 95" in text
    assert "止损 64" in text  # 0.80×0.8 = 0.64


def test_position_sl_hidden_when_disabled():
    """stop_loss=0（关闭止损）→ 不显示止损段。"""
    from pmbot.monitor import render

    st = status_dict(
        position={"direction": "up", "entry_price": 0.50, "size": 5.0,
                  "entered_remaining_sec": 300, "window_start": WINDOW_START},
    )
    tp_sl = {"pct": 0.30, "max": 0.95, "sl": 0.0}
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY, panel=PanelConfig(tp_sl=tp_sl))
    text = render(v)
    assert "止盈 65" in text
    assert "止损" not in text


def test_uptime_shown_bottom():
    """面板底部显示运行时长（HH:MM:SS）。"""
    from pmbot.monitor import render

    v = build_view(status_dict(), trades_rows(), None, now_sec=WINDOW_START, today=TODAY,
                   panel=PanelConfig(uptime_sec=3661))
    text = render(v)
    assert "运行时长 01:01:01" in text
    assert "刷新于" not in text


def test_trade_records_stats_all_trades():
    """交易记录标题显示全部交易统计（超过显示行数也按全量统计）。"""
    from pmbot.monitor import render

    rows = trades_rows(8, pnl=0.55)  # 8 笔 > 显示上限 5 行
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START, today=TODAY)
    assert v.recent_stats["n"] == 8  # 全量，非最近 5 行
    assert v.recent_stats["wins"] == 8
    assert v.recent_stats["losses"] == 0
    assert v.recent_stats["gain"] == pytest.approx(4.40)
    assert v.recent_stats["loss"] == pytest.approx(0.0)
    assert v.recent_stats["max_loss"] == pytest.approx(0.0)
    assert v.recent_stats["pnl"] == pytest.approx(4.40)
    assert len(v.recent_trades) == 5  # 显示行仍取最近 5 条
    text = render(v)
    assert "交易记录（8 笔 · 胜率 100% · 盈亏 +4.40 USDC · 盈利 8 笔 +4.40 · 亏损 0 笔 0.00 · 最大亏损 0.00）:" in text

    rows = trades_rows(3, pnl=0.55) + trades_rows(1, pnl=-0.25)
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START, today=TODAY)
    assert v.recent_stats["n"] == 4
    assert v.recent_stats["wins"] == 3
    assert v.recent_stats["losses"] == 1
    assert v.recent_stats["gain"] == pytest.approx(1.65)
    assert v.recent_stats["loss"] == pytest.approx(-0.25)
    assert v.recent_stats["max_loss"] == pytest.approx(-0.25)
    assert v.recent_stats["pnl"] == pytest.approx(1.40)  # 3×0.55 − 0.25
    text = render(v)
    assert "交易记录（4 笔 · 胜率 75% · 盈亏 +1.40 USDC · 盈利 3 笔 +1.65 · 亏损 1 笔 -0.25 · 最大亏损 -0.25）:" in text


def test_today_line_shows_win_loss_breakdown():
    """今日行显示笔数、盈利/亏损分计与最大亏损。"""
    from pmbot.monitor import render

    rows = trades_rows(5, pnl=0.55)  # 5 笔盈利 +0.55
    rows += trades_rows(1, pnl=-0.30)  # 1 笔亏损 -0.30
    rows += trades_rows(1, pnl=-0.10)  # 1 笔亏损 -0.10
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START, today=TODAY)
    ts = v.today_stats
    assert ts["n"] == 7
    assert ts["wins"] == 5
    assert ts["losses"] == 2
    assert ts["gain"] == pytest.approx(2.75)
    assert ts["loss"] == pytest.approx(-0.40)
    assert ts["max_loss"] == pytest.approx(-0.30)  # 最大亏损单笔
    assert ts["pnl"] == pytest.approx(2.35)
    text = render(v)
    assert "今日盈亏: +2.35 USDC" in text
    assert "今日交易: 7 笔 · 胜率 71% · 盈利 5 笔 +2.75 · 亏损 2 笔 -0.40 · 最大亏损 -0.30" in text


def test_today_line_plain_when_no_trades():
    """无今日交易时只显示盈亏行（余额差口径无标记 = 交易聚合回退）。"""
    from pmbot.monitor import render

    v = build_view(status_dict(), [], None, now_sec=WINDOW_START, today=TODAY)
    assert v.today_stats is None
    text = render(v)
    assert "今日盈亏: +0.00 USDC" in text


def test_recent_trades_stats_hidden_when_empty():
    """无交易时不显示统计（保持原标题）。"""
    from pmbot.monitor import render

    v = build_view(status_dict(), [], None, now_sec=WINDOW_START, today=TODAY)
    assert v.recent_stats is None
    text = render(v)
    assert "交易记录:" in text
    assert "胜率" not in text


def test_recent_trade_reason_chinese():
    """最近交易行的离场原因用中文显示（take_profit → 已止盈）。"""
    from pmbot.monitor import render

    rows = trades_rows(1) + [
        {"ts": "2026-08-14T03:30:00+00:00", "window_start": WINDOW_START,
         "symbol": "BTC", "direction": "up", "entry_price": 0.45, "exit_price": 0.60,
         "size": 5.0, "pnl": 0.75, "reason": "take_profit"},
        {"ts": "2026-08-14T03:31:00+00:00", "window_start": WINDOW_START,
         "symbol": "BTC", "direction": "down", "entry_price": 0.50, "exit_price": 0.30,
         "size": 5.0, "pnl": -1.0, "reason": "window_end"},
    ]
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START, today=TODAY)
    text = render(v)
    assert "已止盈" in text
    assert "窗口结束平仓" in text
    assert "take_profit" not in text
    assert "window_end" not in text


def test_position_size_two_decimals():
    """持仓份额只显示小数点后 2 位（1.4925... → 1.49）。"""
    from pmbot.monitor import render

    st = status_dict(
        position={"direction": "up", "entry_price": 0.67, "size": 1.4925373134328357,
                  "entered_remaining_sec": 300, "window_start": WINDOW_START},
        market_prices={"up_ask": 0.71, "up_bid": 0.70, "down_ask": 0.54, "down_bid": 0.53},
    )
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START, today=TODAY)
    text = render(v)
    assert "持仓: UP 1.49股 @67" in text
    assert "现价 70" in text
    assert "1.4925" not in text


def test_window_label_uses_injected_interval():
    """5m 窗口标签与剩余秒按注入的窗口长度计算。"""
    v = build_view(status_dict(), trades_rows(), None, now_sec=WINDOW_START + 60,
                   today=TODAY, local_tz=timezone.utc, panel=PanelConfig(window_seconds=300))
    assert v.window_label == "08-14 03:30-03:35"
    assert v.window_remaining_sec == 300 - 60


def test_render_shows_config_summary():
    from pmbot.monitor import render

    v = build_view(status_dict(), trades_rows(), None, now_sec=WINDOW_START, today=TODAY,
                   panel=PanelConfig(config_summary="kronos | BTC | 5m | 注1.0 | 限价0.40 | 止盈0.70 | 止损0.15"))
    text = render(v)
    assert "配置:" in text
    assert "限价0.40" in text


def test_predicting_state_shown():
    from pmbot.monitor import render

    st = status_dict(last_predict_sec=None, signal=None)
    st.predicting = True
    st.predict_start_sec = WINDOW_START + 5
    v = build_view(st, trades_rows(), None, now_sec=WINDOW_START + 20, today=TODAY)
    text = render(v)
    assert "推理中" in text
    assert "15 秒" in text


def test_read_book_prices_fresh(tmp_path, monkeypatch):
    """book.json 新鲜（3s 内）→ 返回实时盘口。"""
    from pmbot.monitor import _read_book_prices

    d = tmp_path / "data"
    d.mkdir()
    p = d / "book.json"
    p.write_text(json.dumps({"ts": time.time() * 1000, "up_ask": 0.11, "up_bid": 0.1,
                             "down_ask": 0.9, "down_bid": 0.89}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    got = _read_book_prices()
    assert got == {"up_ask": 0.11, "up_bid": 0.1, "down_ask": 0.9, "down_bid": 0.89}


def test_read_book_prices_stale_or_missing(tmp_path, monkeypatch):
    """过期/缺失 → None（回退 status.json）。"""
    from pmbot.monitor import _read_book_prices

    d = tmp_path / "data"
    d.mkdir()
    p = d / "book.json"
    p.write_text(json.dumps({"ts": (time.time() - 60) * 1000, "up_ask": 0.11}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _read_book_prices() is None
    p.unlink()
    assert _read_book_prices() is None


def test_recent_limit_none_returns_all_trades():
    rows = trades_rows(n=8)
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START, recent_limit=None)
    assert len(v.recent_trades) == 8  # Web 控制台全量


def test_recent_limit_default_truncated():
    rows = trades_rows(n=8)
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START)
    assert len(v.recent_trades) == 5


def test_recent_trade_has_chinese_label():
    rows = trades_rows(n=1)
    rows[0]["reason"] = "take_profit"
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START)
    assert v.recent_trades[0]["label"] == "已止盈"


def test_fmt_cents_low_price_keeps_precision():
    """0.1 美分价格不得显示为 0（如 entry=0.001 → 0.10 美分）。"""
    from pmbot.monitor import _fmt_cents

    assert _fmt_cents(0.001) == "0.10"
    assert _fmt_cents(0.008) == "0.80"
    assert _fmt_cents(0.01) == "1"
    assert _fmt_cents(0.45) == "45"
    assert _fmt_cents(0.8) == "80"


def test_render_trade_low_entry_shows_fractional_cents():
    """交易行低入场价显示小数美分（入0.10 而非入0）。"""
    from pmbot.monitor import render

    rows = trades_rows(n=1)
    rows[0].update({"entry_price": 0.001, "exit_price": 0.8, "pnl": 799.0, "reason": "take_profit"})
    v = build_view(status_dict(), rows, None, now_sec=WINDOW_START)
    text = render(v)
    assert "入0.10 出80" in text
    assert "入0 " not in text


def test_position_carries_tp_sl_percent_source():
    """持仓止盈止损同时携带百分比来源（web 显示标注用）。"""
    v = build_view(status_dict(), trades_rows(), None, now_sec=WINDOW_START,
                   panel=PanelConfig(tp_sl={"pct": 0.50, "max": 0.90, "sl": 0.60}))
    pos = v.position
    assert pos["tp_price"] == 0.45 * 1.50
    assert pos["tp_pct"] == 0.50
    assert pos["sl_pct"] == 0.60


def test_view_carries_spot_price():
    """视图携带交易品种实时价快照（web 顶栏渲染用）。"""
    v = build_view(status_dict(), trades_rows(), None, now_sec=WINDOW_START,
                   panel=PanelConfig(spot={"price": 2345.67, "delta": 12.3}))
    assert v.spot == {"price": 2345.67, "delta": 12.3}
    v2 = build_view(status_dict(), trades_rows(), None, now_sec=WINDOW_START)
    assert v2.spot is None


def test_spot_price_snapshot_and_delta():
    """SpotPrice 快照与涨跌差值（首次拉取后无 delta，之后有）。"""
    sp = SpotPrice()
    assert sp.snapshot() is None  # 尚未拉取
    sp._price = 2000.0
    sp._delta = 0.0
    assert sp.snapshot() == {"price": 2000.0, "delta": 0.0}
    sp._tick = lambda: None  # 防线程启动干扰
    sp._price = 2010.0
    sp._delta = 10.0
    assert sp.snapshot() == {"price": 2010.0, "delta": 10.0}


def test_live_view_uses_config_interval(tmp_path, monkeypatch):
    """_build_live_view 必须用 config 参数（不是 args 残留）：窗口标签按 config 的 5m 对齐。

    回归：签名解耦（候选 C）后函数体内残留 args.config → NameError 被
    except 吞掉 → window_seconds 回退默认 900s（显示 15m 窗口标签）、
    config_summary 为空。
    """
    from pmbot.monitor import _build_live_view
    from pmbot.paths import RuntimePaths

    (tmp_path / "status.json").write_text('{"symbol": "ETH", "window_start": 1786872300}',
                                         encoding="utf-8")
    v = _build_live_view("ETH", "config.yaml", RuntimePaths(data_dir=str(tmp_path)))
    assert v.window_label.endswith("-17:30")  # 17:25-17:30（5m 对齐）
    assert "5m" in v.config_summary  # config 读取未失败
