"""终端监控面板（只读 TUI）：并行于主循环，每 2 秒刷新运行状态。

用法: uv run python -m pmbot.monitor [--status data/status.json]
数据只读自 status.json / trades.csv / PredictionLog，不改主循环。
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pmbot.constants import WINDOW_SECONDS, step_ms_for
from pmbot.state import StateStore, TradeState

REFRESH_SEC = 1.0
RECENT_LIMIT = 5

# 平仓原因 → 面板状态文案（trades.csv reason 字段）
EXIT_LABELS = {
    "take_profit": "已止盈",
    "stop_loss": "已止损",
    "time_stop": "已时间止损",
    "window_end": "窗口结束平仓",
    "settle": "已结算",
    "sell": "已平仓",
}


@dataclass(frozen=True)
class PanelConfig:
    """面板展示配置（config 注入项打包，避免 build_view 参数膨胀）。"""

    model_variant: str = "—"
    thresholds: dict | None = None
    window_seconds: int = WINDOW_SECONDS
    config_summary: str = ""
    tp_sl: dict | None = None
    uptime_sec: int | None = None
    spot: dict | None = None  # 交易品种实时价 {"price", "delta"}


class SpotPrice:
    """交易品种实时价轮询（Binance 公共数据镜像，与 K 线同源；失败静默保留旧值）。"""

    def __init__(self, symbol: str = "ETH", interval: float = 3.0):
        self.symbol = symbol
        self.interval = interval
        self._lock = threading.Lock()
        self._price: float | None = None
        self._delta: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="spot-price", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict | None:
        """当前价格快照；尚无成功拉取返回 None。"""
        with self._lock:
            if self._price is None:
                return None
            return {"price": self._price, "delta": self._delta}

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._tick()

    def _tick(self) -> None:
        import requests

        try:
            r = requests.get(
                f"https://data-api.binance.vision/api/v3/ticker/price?symbol={self.symbol}USDT",
                timeout=5,
            )
            r.raise_for_status()
            price = float(r.json()["price"])
        except Exception:
            return  # 网络失败：静默保留旧值
        with self._lock:
            if self._price is not None:
                self._delta = price - self._price
            self._price = price


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fmt_ts(ts: str) -> str:
    """ISO 时间 → 本地时区 MM-DD HH:MM（astimezone(None) = 系统本地时区）。"""
    return _parse_ts(ts).astimezone(None).strftime("%m-%d %H:%M")


def _fmt_cents(x: float) -> str:
    """价格（0-1 概率）→ 美分显示；不足 1 美分保留 2 位小数（防 0.1 美分显示成 0）。"""
    c = x * 100
    return f"{c:.2f}" if c < 1 else f"{c:.0f}"


def _today_local(tz=None) -> str:
    """本地时区（默认系统本地）今日日期串（YYYY-MM-DD），自然日切分。"""
    return datetime.now(timezone.utc).astimezone(tz).strftime("%Y-%m-%d")


def _read_book_prices(data_dir: str | Path = "data", max_age_sec: float = 3.0) -> dict | None:
    """读取 BookSampler 落盘的实时盘口（<data-dir>/book.json）；过期/缺失返回 None。"""
    try:
        p = Path(data_dir) / "book.json"
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - (data.get("ts", 0) / 1000) > max_age_sec:
            return None
        return {k: data[k] for k in ("up_ask", "up_bid", "down_ask", "down_bid") if k in data}
    except Exception:
        return None


@dataclass
class PanelView:
    """展示视图契约：build_view 输出，TUI render 消费（类型化，禁止魔法键）。

    字段名即 JSON 键名（web 控制台经 asdict 边界转换），键名唯一出处在本类。
    嵌套展示结构（signal/position/prices 等）保留 dict——它们是状态快照的展示投影。
    """

    symbol: str = "—"
    window_label: str = "—"
    window_remaining_sec: int | None = None
    signal: dict | None = None
    signal_note: str = ""
    status_note: str = ""
    pending: dict | None = None
    position: dict | None = None
    settle_pending: dict | None = None  # 窗口结束后待结算的旧持仓（结算完成自动清空）
    paused: bool = False
    pause_reason: str | None = None
    consecutive_losses: int = 0
    daily_loss: float = 0.0
    today_pnl: float = 0.0
    today_pnl_src: str = ""  # 今日盈亏口径："balance"=按钱包余额（实盘真实资金变动），""=按交易记录聚合回退
    today_trades: int = 0
    today_stats: dict | None = None
    recent_trades: list = field(default_factory=list)
    recent_stats: dict | None = None
    accuracy: dict | None = None
    model_variant: str = "—"
    last_predict_sec: int | None = None
    predicting: bool = False
    predict_start_sec: int | None = None
    prices: dict | None = None
    live_positions: list = field(default_factory=list)  # Polymarket 实时持仓（/positions 快照）
    config_summary: str = ""
    tp_sl: dict | None = None
    uptime_sec: int | None = None
    startup_wait_sec: int | None = None  # 启动跳过进行中窗口的剩余等待秒（面板提示，避免误判启动失败）
    now_sec: int = 0
    last_updated: str = ""
    balance: float | None = None
    spot: dict | None = None
    mode: str = ""  # dry-run / live（面板数据来源模式）
    loop_alive: bool | None = None
    show_tui: bool = True


def _view_defaults(accuracy, model_variant, tp_sl, uptime_sec, now_sec, config_summary) -> PanelView:
    """视图默认值（build_view 入口骨架）。"""
    return PanelView(
        accuracy=accuracy,
        model_variant=model_variant,
        tp_sl=tp_sl,
        uptime_sec=uptime_sec,
        now_sec=now_sec,
        config_summary=config_summary,
    )


def _fill_status_note(v: PanelView, status: TradeState, trades: list["TradeRecord"]) -> None:
    """运行状态说明：按持仓/挂单/本窗口平仓/信号状态生成状态文案。"""
    # 当前窗口是否已有平仓记录（trades 按时间追加，最后一条匹配的即最近）
    window_trade = None
    for r in trades:
        if r.window_start == status.window_start:
            window_trade = r
    if v.position is not None:
        v.status_note = "持仓中"
    elif v.pending is not None:
        v.status_note = "挂单中（等回调）"
    elif window_trade is not None:
        # 本窗口已平仓：按实际离场原因显示（止盈/止损/时间止损/结算）
        v.status_note = EXIT_LABELS.get(window_trade.reason, "已平仓")
    elif v.signal_note.startswith("买入方向"):
        v.status_note = "挂单已撤（窗口未成交）"
    elif v.signal_note:
        v.status_note = "观望（未达阈值）"


def _aggregate_trades(trades, match=None) -> dict:
    """聚合交易统计：委托 stats.aggregate（单一实现，面板/验证报告同口径）。

    match(record) 返回 False 的行不计入（如今日过滤）；None 表示全部交易。
    """
    from pmbot.stats import aggregate

    return aggregate(trades, match)


def _fill_trades(v: PanelView, trades: list["TradeRecord"], today: str, tz,
                recent_limit: int | None = RECENT_LIMIT) -> None:
    """今日统计/最近交易/全部交易统计（坏行已在账本读面滤除）。

    recent_limit=None 表示返回全部交易（Web 控制台用）；默认截断 RECENT_LIMIT 条。
    """
    today_stats = _aggregate_trades(
        trades,
        match=lambda r: _parse_ts(r.ts).astimezone(tz).strftime("%Y-%m-%d") == today,
    )
    v.today_pnl = round(today_stats["pnl"], 4)
    v.today_trades = today_stats["n"]
    v.today_stats = today_stats if today_stats["n"] else None
    recent = []
    limit = trades if recent_limit is None else trades[-recent_limit:]
    for r in reversed(limit):  # 降序：最新交易在上（前端分页展示）
        recent.append(
            {
                "ts": _fmt_ts(r.ts),
                "direction": r.direction,
                "entry": r.entry_price,
                "exit": r.exit_price,
                "pnl": round(r.pnl, 4),
                "reason": r.reason,
                "label": EXIT_LABELS.get(r.reason, r.reason),
            }
        )
    v.recent_trades = recent


def build_view(status: TradeState | None, trades: list["TradeRecord"], accuracy: dict | None,
               now_sec: int, today: str | None = None, local_tz=None,
               panel: PanelConfig | None = None, recent_limit: int | None = RECENT_LIMIT) -> PanelView:
    """从 TradeState / trades / PredictionLog 构建视图数据（纯函数）。

    today 注入 UTC 日期串（YYYY-MM-DD），测试可固定时钟；
    local_tz 注入显示时区（默认 None = 系统本地时区）；
    panel 注入面板展示配置（模型变体/下注阈值/窗口长度/配置摘要/止盈止损/运行时长）；
    recent_limit=None 返回全部交易历史（Web 控制台用）。
    """
    today = today or _today_local()
    tz = local_tz  # None → astimezone 用系统本地时区
    panel = panel or PanelConfig()
    th = panel.thresholds or {"p_up_buy": 0.60, "p_down_buy": 0.40}
    v = _view_defaults(accuracy, panel.model_variant, panel.tp_sl, panel.uptime_sec,
                       now_sec, panel.config_summary)
    if status:
        v.symbol = status.symbol
        ws = status.window_start
        if ws:
            start = datetime.fromtimestamp(ws, tz=timezone.utc).astimezone(tz)
            end = start + timedelta(seconds=panel.window_seconds)
            v.window_label = f"{start:%m-%d %H:%M}-{end:%H:%M}"
            v.window_remaining_sec = max(0, int(start.timestamp() + panel.window_seconds - now_sec))
        if status.signal:
            s = status.signal
            v.signal = {"direction": s.direction.value, "p_up": s.p_up}
            p_up = v.signal["p_up"]
            if p_up is not None:
                if p_up > th["p_up_buy"]:
                    v.signal_note = "买入方向：up"
                elif p_up < th["p_down_buy"]:
                    v.signal_note = "买入方向：down"
                else:
                    v.signal_note = "未达阈值，跳过"
        if status.pending_order:
            p = status.pending_order
            v.pending = {"direction": p.direction.value, "price": p.price, "size": p.size}
        if status.position:
            p = status.position
            v.position = {"direction": p.direction.value, "entry_price": p.entry_price, "size": p.size}
            # 止盈/止损价（与引擎同一公式 position_exit_levels，避免 UI 公式漂移）
            if panel.tp_sl:
                from pmbot.exit_rules import position_exit_levels

                tp, sl = position_exit_levels(
                    p.entry_price, panel.tp_sl["pct"], panel.tp_sl["sl"],
                    tp_max=panel.tp_sl["max"],
                )
                v.position["take_profit_price"] = tp
                v.position["stop_loss_price"] = sl
        if status.settle_pending:
            p = status.settle_pending
            v.settle_pending = {
                "direction": p.direction.value,
                "entry_price": p.entry_price,
                "size": p.size,
                "window_start": p.window_start,
            }
        v.paused = bool(status.paused)
        v.pause_reason = status.pause_reason
        v.consecutive_losses = status.consecutive_losses
        v.daily_loss = status.daily_loss
        v.last_predict_sec = status.last_predict_sec
        v.predicting = bool(status.predicting)
        v.predict_start_sec = status.predict_start_sec
        v.prices = status.market_prices
        # 启动跳过进行中窗口：剩余等待秒（>0 时面板提示，避免误以为启动失败）
        if status.skip_until_sec and status.skip_until_sec > now_sec:
            v.startup_wait_sec = status.skip_until_sec - now_sec
        v.live_positions = status.live_positions or []
        v.config_summary = panel.config_summary
        v.spot = panel.spot
        v.balance = status.balance
        _fill_status_note(v, status, trades)

    _fill_trades(v, trades, today, tz, recent_limit)
    # 交易记录统计：全部交易（笔数/胜率/盈亏/盈利与亏损分计/最大亏损），非仅最近显示的行
    total = _aggregate_trades(trades)
    v.recent_stats = total if total["n"] else None
    v.last_updated = datetime.now(timezone.utc).astimezone(tz).strftime("%H:%M:%S")
    return v


def _fmt_uptime(sec: int | None) -> str:
    """秒 → HH:MM:SS（None → --:--:--）。"""
    if sec is None:
        return "--:--:--"
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def render(v: PanelView) -> str:
    """渲染为纯文本面板（rich 负责着色，此处保证结构）。"""
    lines = ["=" * 62, "PMBOT 运行状态", "=" * 62]
    if v.config_summary:
        lines.append(f"配置: {v.config_summary}")
    lines.append(
        f"标的: {v.symbol}    暂停: {'⚠️ 是（' + v.pause_reason + '）' if v.paused and v.pause_reason else ('⚠️ 是' if v.paused else '否')}"
    )
    if v.predicting:
        secs = v.now_sec - v.predict_start_sec if v.predict_start_sec else 0
        kronos_state = f"🔄 推理中（已 {max(0, secs)} 秒）"
    else:
        inferred = v.last_predict_sec or v.signal is not None  # 有信号即已推理（兼容旧 status）
        kronos_state = "✅ 已推理" if inferred else "⏳ 等待首次推理"
    lines.append(f"模型: {v.model_variant}    Kronos: {kronos_state}")
    if v.last_predict_sec:
        t = datetime.fromtimestamp(v.last_predict_sec, tz=timezone.utc).astimezone(None)
        lines.append(f"上次预测: {t:%m-%d %H:%M:%S}（本地）")
    lines.append(f"当前窗口: {v.window_label}")
    prices = v.prices
    if prices:
        def fmt(x):
            # 美分单位（×100）：与 Polymarket web 盘口 55/45 风格对齐；不足 1 美分保留小数
            return _fmt_cents(x) if x is not None else "—"

        lines.append(f"盘口 UP: {fmt(prices.get('up_bid'))}/{fmt(prices.get('up_ask'))}  "
                     f"DOWN: {fmt(prices.get('down_bid'))}/{fmt(prices.get('down_ask'))}（买/卖）")
    rem = v.window_remaining_sec
    lines.append(f"窗口剩余: {f'{rem // 60}分{rem % 60:02d}秒' if rem is not None else '—'}")
    if v.startup_wait_sec is not None:
        m, s = divmod(v.startup_wait_sec, 60)
        lines.append(f"启动等待: ⏳ 跳过进行中窗口，{m}分{s:02d}秒后开始交易")
    sig = v.signal
    if sig:
        note = f" [{v.signal_note}]" if v.signal_note else ""
        lines.append(f"信号: {sig['direction'].upper()} (P(up)={sig['p_up']:.2f}){note}")
    if v.status_note:
        lines.append(f"状态: {v.status_note}")
    else:
        lines.append("信号: —")
    pend = v.pending
    if pend:
        lines.append(f"挂单: {pend['direction'].upper()} {pend['size'] or '?'}股 @{_fmt_cents(pend['price'])}")
    else:
        lines.append("挂单: —")
    pos = v.position
    if pos:
        # 持仓只显示方向/数量/价格三项（用户口径）
        lines.append(f"持仓: {pos['direction'].upper()} {pos['size']:.2f}股 @{_fmt_cents(pos['entry_price'])}")
    else:
        lines.append("持仓: —")
    if v.settle_pending:
        sp = v.settle_pending
        lines.append(
            f"待结算: {sp['direction'].upper()} {sp['size']:.2f}股 @{_fmt_cents(sp['entry_price'])}（窗口已结束，结算后自动入账）"
        )
    if v.live_positions:
        lines.append("实时持仓（Polymarket）:")
        for p in v.live_positions[:5]:
            try:
                size = float(p.get("size") or 0)
                avg = float(p.get("avgPrice") or 0)
                cur = float(p.get("curPrice") or 0)
                pnl = float(p.get("cashPnl") or 0)
                title = str(p.get("title") or "")[:34]
                lines.append(
                    f"  {title:34s} {size:.4f}股 均{avg:.3f} 现{cur:.3f} 浮动{pnl:+.2f}"
                )
            except (TypeError, ValueError):
                continue
    lines.append(f"连亏: {v.consecutive_losses} 笔")
    lines.append("-" * 62)
    ts = v.today_stats
    pnl_src = "（按钱包余额）" if v.today_pnl_src == "balance" else "（按交易记录）"
    lines.append(f"今日盈亏: {v.today_pnl:+.2f} USDC{pnl_src}")
    if ts:
        lines.append(
            f"今日交易: {ts['n']} 笔 · 胜率 {ts['wins'] / ts['n']:.0%} · "
            f"盈利 {ts['wins']} 笔 +{ts['gain']:.2f} · 亏损 {ts['losses']} 笔 {ts['loss']:.2f} · "
            f"最大亏损 {ts['max_loss']:.2f}"
        )
    lines.append("-" * 62)
    rs = v.recent_stats
    if rs:
        lines.append(
            f"交易记录（{rs['n']} 笔 · 胜率 {rs['wins'] / rs['n']:.0%} · 盈亏 {rs['pnl']:+.2f} USDC · "
            f"盈利 {rs['wins']} 笔 +{rs['gain']:.2f} · 亏损 {rs['losses']} 笔 {rs['loss']:.2f} · "
            f"最大亏损 {rs['max_loss']:.2f}）:"
        )
    else:
        lines.append("交易记录:")
    if v.recent_trades:
        for t in v.recent_trades:
            lines.append(
                f"  {t['ts']}  {t['direction'].upper():4} 入{_fmt_cents(t['entry'])} 出{_fmt_cents(t['exit'])} "
                f"{t['pnl']:+.2f}  {EXIT_LABELS.get(t['reason'], t['reason'])}"
            )
    else:
        lines.append("  （暂无）")
    acc = v.accuracy
    if acc and acc["total"]:
        lines.append(f"方向准确率: {acc['accuracy']:.1%} ({acc['correct']}/{acc['total']})")
    else:
        lines.append("方向准确率: —（暂无评估样本）")
    lines.append(f"运行时长 {_fmt_uptime(v.uptime_sec)}")
    bal = v.balance
    lines.append(f"钱包余额: {bal if bal is not None else '—'} USDC")
    mode = v.mode
    if mode:
        lines.append(f"运行模式: {'🟢 实盘' if mode == 'live' else '🟡 模拟（dry-run）'}")
    loop = v.loop_alive
    if loop is not None:
        lines.append(f"主循环: {'🟢 运行中' if loop else '🔴 已停止'}")
    return "\n".join(lines)


def _build_live_view(symbol: str | None, config: str, paths: RuntimePaths,
                     recent_limit: int | None = RECENT_LIMIT,
                     uptime_sec: int | None = None, spot: dict | None = None) -> PanelView:
    """读取状态/交易/盘口/配置并构建视图（TUI 与 Web 控制台共用）。

    symbol: 交易品种（None 时从 status.json 回退）；config: 配置文件路径。
    paths: 运行路径单一事实源（模式切换后自动跟随新数据目录）。
    spot: 交易品种实时价快照（{"price", "delta"}；Web 控制台由 SpotPrice 轮询提供）。
    """
    st = StateStore(paths.status).load()
    if st is not None:
        book = _read_book_prices(paths.log_dir)
        if book is not None:
            st.market_prices = book
    trades = []
    # 统一读面（账本）：实盘 api_trades.csv（真实流水配对，含手续费）优先，
    # 缺回退 trades.csv（引擎业务记录）；dry-run 目录无 api 流水自然回退。
    # 判据唯一（ledger.load_records），消费方不再各自猜文件。
    from pmbot.ledger import load_records

    trades = load_records(paths.data_dir)

    symbol = symbol or (st.symbol if st else None)
    from pmbot.config import load_config
    from pmbot.prediction_log import PredictionLog

    model_variant = "—"
    thresholds = None
    window_seconds = WINDOW_SECONDS
    config_summary = ""
    tp_sl = None
    try:
        cfg = load_config(config)
        model_variant = cfg.model_variant
        thresholds = {"p_up_buy": cfg.p_up_buy, "p_down_buy": cfg.p_down_buy}
        window_seconds = step_ms_for(cfg.market_interval) // 1000
        tp_sl = {"pct": cfg.take_profit, "max": cfg.take_profit_max, "sl": cfg.stop_loss}
        config_summary = (
            f"{cfg.strategy} | {','.join(cfg.symbols)} | {cfg.market_interval} | "
            f"注{cfg.amount_per_trade} | P(up)≥{cfg.p_up_buy} | "
            f"止盈+{cfg.take_profit * 100:.0f}%（封顶{cfg.take_profit_max:.2f}） | "
            f"止损-{cfg.stop_loss * 100:.0f}% | 亏损离场{cfg.exit_loss_before_end_sec}s | "
            f"盈利持有{cfg.hold_until_end_sec}s | 禁入{cfg.no_entry_before_end_sec}s | "
            f"开仓延迟{cfg.open_delay_sec}s | "
            f"连亏熔断{cfg.max_consecutive_losses} | 日亏熔断{cfg.max_daily_loss}"
        )
    except Exception:
        pass
    acc = PredictionLog(paths.log_dir, symbol).accuracy() if symbol else None
    v = build_view(st, trades, acc, now_sec=int(time.time()),
                   panel=PanelConfig(
                       model_variant=model_variant, thresholds=thresholds,
                       window_seconds=window_seconds, config_summary=config_summary,
                       tp_sl=tp_sl, uptime_sec=uptime_sec, spot=spot),
                   recent_limit=recent_limit)
    v.mode = paths.mode  # 面板数据来源模式（防混淆）
    return v


def main(argv: list[str] | None = None) -> int:
    from pmbot.web_ui import WEB_PORT

    parser = argparse.ArgumentParser(description="PMBOT 终端监控面板（只读 TUI + Web 控制台）")
    parser.add_argument("--status", default=None, help="状态文件（默认 <data-dir>/status.json）")
    parser.add_argument("--trades", default=None, help="交易日志（默认 <data-dir>/trades.csv）")
    parser.add_argument("--symbol", default=None, help="预测日志标的（默认取 status 的 symbol）")
    parser.add_argument("--log-dir", default=None, help="预测日志目录（默认 <data-dir>）")
    parser.add_argument("--data-dir", default="data", help="数据目录（与主循环 --data-dir 一致）")
    parser.add_argument("--refresh", type=float, default=REFRESH_SEC, help="刷新间隔秒（默认 1.0）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（Web 启动主循环时用）")
    parser.add_argument("--live", dest="live", action="store_true", help="实盘模式（Web 启动主循环时用）")
    parser.add_argument("--dry-run", dest="live", action="store_false", help="模拟模式（默认）")
    parser.set_defaults(live=False)
    parser.add_argument("--web-port", type=int, default=WEB_PORT, help="Web 控制台端口（-1 禁用）")
    parser.add_argument("--web-host", default="0.0.0.0",
                        help="Web 控制台绑定地址（默认 0.0.0.0 局域网可访问；写操作需 token）")
    parser.add_argument("--web-only", action="store_true",
                        help="只跑 Web 控制台，不渲染终端面板（start_bot 默认）")
    args = parser.parse_args(argv)
    # 显式路径参数可覆盖派生；未指定时基于 data-dir 派生（与主循环同目录）
    from pmbot.config import load_config as _load_config
    from pmbot.paths import ProcessControl, RuntimePaths, paths_for

    paths = RuntimePaths(
        data_dir=args.data_dir,
        mode="live" if args.live else "dry-run",
        status=args.status,
        trades=args.trades,
        log_dir=args.log_dir,
    )

    from rich.console import Console
    from rich.live import Live

    import threading
    import webbrowser

    console = Console()
    console.print("[bold cyan]PMBOT 监控面板启动（Ctrl-C 退出）[/]")
    pc = ProcessControl(show_tui=not args.web_only, live=args.live, paths=paths)
    lock = threading.Lock()
    t_start = time.monotonic()
    server = None
    spot = SpotPrice(symbol=args.symbol or _load_config(args.config).symbols[0])

    def apply_mode(live: bool) -> None:
        """切换面板/启动主循环的运行模式（模拟 ↔ 实盘，含数据目录切换）。

        单一事实源：pc.paths 换新，路径派生与展示模式全部跟随。
        """
        pc.live = live
        pc.paths = paths_for(live)

    if args.web_port > 0:
        import os
        import secrets

        from pmbot.web_ui import WEB_PORT, start_server

        # 写操作访问令牌：优先环境变量，否则读/生成 data/web_token.txt
        token = os.environ.get("PMBOT_WEB_TOKEN", "")
        token_file = Path("data") / "web_token.txt"
        if not token:
            try:
                if token_file.is_file():
                    token = token_file.read_text(encoding="utf-8").strip()
                if not token:
                    token = secrets.token_urlsafe(16)
                    token_file.write_text(token, encoding="utf-8")
            except OSError:
                token = ""

        spot.start()
        server = start_server(
            lambda: _build_live_view(args.symbol, args.config, pc.paths, recent_limit=None,
                                     uptime_sec=int(time.monotonic() - t_start),
                                     spot=spot.snapshot()),
            args.config, args.live, args.web_port, args.web_host, pc, lock,
            token=token,
            set_mode_fn=apply_mode,
        )
        url = f"http://127.0.0.1:{args.web_port}"
        console.print(f"[bold green]Web 控制台: {url}[/]")
        if args.web_host not in ("127.0.0.1", "localhost"):
            import socket

            try:
                ip = socket.gethostbyname(socket.gethostname())
                console.print(f"[bold green]局域网访问: http://{ip}:{args.web_port}?token={token}[/]")
            except OSError:
                pass
        if args.web_only:
            try:
                webbrowser.open(url)  # 本机回环访问免 token（局域网访问需带 token，见启动日志）
            except Exception:
                pass
            console.print("[dim]终端面板已隐藏；可在 Web 控制台「终端面板」按钮打开[/]")
    try:
        with Live(console=console, refresh_per_second=1 / args.refresh, screen=False) as live:
            while True:
                if pc.show_tui:
                    try:
                        v = _build_live_view(args.symbol, args.config, pc.paths,
                                             uptime_sec=int(time.monotonic() - t_start))
                        if args.web_port > 0:
                            v.loop_alive = pc.loop_alive()
                        live.update(render(v))
                    except Exception as e:  # 只读面板：任何异常都不崩溃，显示错误继续
                        live.update(f"读取失败: {e}")
                time.sleep(args.refresh)
    except KeyboardInterrupt:
        console.print("\n[dim]监控退出[/]")
    finally:
        if server is not None:
            server.shutdown()
        proc = pc.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
