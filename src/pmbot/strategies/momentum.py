"""动量/Breakout 策略：Binance 实时价相对窗口开盘穿越阈值后同向入场。

研究依据：docs/reports/momentum_strategy.md（2026-09-04）
- 5m/15m 窗口内 BTC 有强动量：价格穿越阈值后方向延续 72-97%
  （binance 5m 8000+ 根 / 15m 6000+ 根大样本，去 selection bias 仍显著）
- opening-direction 不可预测（50%，Kronos/XGBoost/LightGBM 全方位证伪），
  但 path-dependent 动量可交易：开盘不可预测，但"已穿越 X%"是强条件信息
- 策略：窗口开盘记录基准价 → 实时监控 Binance 价 → 穿越 +X% 买 Up、
  穿越 -X% 买 Down → 每窗口最多 1 笔（引擎 window_bet_placed 保证）→
  持有到结算（引擎止盈 0.95 近似持有，赢收 1.0 输收 0，redeem 无费）
- 扣费期望（Polymarket 真实费率：taker 买入费 = 0.07×p×(1-p)/股，结算无费）：
  5m EV +0.15~0.40/笔，15m EV +0.13~0.35/笔（见报告）

与 Kronos 的差异：Kronos 在窗口开盘一次性推理预测方向（opening-direction，
50% 无 edge）；momentum 在窗口内持续检测穿越（refresh_signal 钩子），
信号可随时更新——这是 path edge 的可执行形态。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from pmbot.config import StrategyConfig
from pmbot.constants import step_ms_for
from pmbot.data_source import fetch_klines_batch, normalize_symbol
from pmbot.strategy import Strategy, register
from pmbot.types import Direction, Signal, SignalContext


# Binance 实时价 REST（与 SpotTickerThread 同源镜像；强制直连，实测大陆可达）
_PRICE_URL = "https://data-api.binance.vision/api/v3/ticker/price?symbol={sym}"


def _fetch_price_rest(symbol: str) -> float | None:
    """Binance 实时价（REST ticker/price）。失败返回 None（退避，不崩溃）。"""
    import requests

    try:
        r = requests.get(_PRICE_URL.format(sym=normalize_symbol(symbol)), timeout=5,
                         proxies={"http": None, "https": None})
        return float(r.json()["price"])
    except Exception:
        return None


@register("momentum")
class MomentumStrategy(Strategy):
    # 构造前依赖注入声明（类属性，run.py 查询）：需要 Binance 实时价注入 fetch_price
    # （WS ~1s，比每 tick REST 快；更早发现穿越 → 更可能抓做市商未调价的便宜档）
    needs_fetch_price = True

    """穿越阈值后同向入场的动量策略（窗口内事件驱动）。

    generate_signal 在窗口开始调用：建窗口基准价（当前窗口 K 的 open），
    立即检测一次穿越；refresh_signal 每 tick 调用：实时监控穿越，
    穿越即返回同向信号（引擎随即入场）。每窗口至多入场一次。
    """

    def __init__(
        self,
        *,
        strategy_config: StrategyConfig | None = None,
        symbol: str = "BTC",
        log_dir: str | Path = "data",
        fetch_price: Callable[[], float | None] | None = None,
        fetch_window_open: Callable[[], float | None] | None = None,
        fetch_btc_price: Callable[[], float | None] | None = None,
    ) -> None:
        sc = strategy_config or StrategyConfig()
        self.symbol = symbol
        self.interval = sc.market_interval
        self.step_ms = step_ms_for(self.interval)
        # 分标的穿越阈值：threshold_by_symbol 覆盖默认 threshold_pct
        # （SOL 波动 > ETH > BTC，同样 % 下 SOL 假穿越多，需更高阈值）
        tb = getattr(sc, "threshold_by_symbol", None) or {}
        self.threshold_pct = tb.get(symbol, getattr(sc, "threshold_pct", 0.08))
        # BTC 反向矛盾过滤阈值（0 = 关闭）：标的穿越方向与 BTC 反向时跳过
        self.btc_contradiction_pct = getattr(sc, "btc_contradiction_pct", 0.0)
        # 依赖注入（测试用 fake；运行态默认走 Binance REST）
        self._fetch_price = fetch_price or (lambda: _fetch_price_rest(symbol))
        self._fetch_window_open = fetch_window_open or self._default_window_open
        self._fetch_btc_price = fetch_btc_price or (lambda: _fetch_price_rest("BTC"))
        # 窗口基准价（收盘/穿越判断的锚）：None = 当前窗口尚未建立基准
        self._base: float | None = None
        # BTC 窗口基准价（反向过滤的锚）：None = 尚未建立
        self._btc_base: float | None = None
        # 最近一次实时价（面板状态展示）
        self._last_price: float | None = None

    # ---- 依赖注入默认实现 ----

    def _fetch_symbol_window_open(self, symbol: str) -> float | None:
        """当前窗口 K 线的 open（窗口起点价，与 Polymarket 结算基准同源）。"""
        try:
            now_ms = int(time.time() * 1000)
            ks = fetch_klines_batch(
                symbol, self.interval, since=now_ms - 2 * self.step_ms,
                limit=4, proxies=None,
            )
            if not ks:
                return None
            return float(ks[-1].open)
        except Exception:
            return None

    def _default_window_open(self) -> float | None:
        """当前窗口 K 线的 open（窗口起点价，与 Polymarket 结算基准同源）。

        当前进行中的窗口 K（binance interval=market_interval 的最后一根）
        的 open 在窗口开始即确定——即使窗口已进行几分钟，open 仍是窗口
        起点价，延迟重建基准不引入偏差。
        """
        return self._fetch_symbol_window_open(self.symbol)

    # ---- 信号 ----

    def _deviation_pct(self, price: float) -> float | None:
        """实时价相对窗口基准的偏离百分比（0.5 = +0.5%）。基准缺失返回 None。"""
        if self._base is None or self._base <= 0:
            return None
        return (price - self._base) / self._base * 100.0

    def _signal_for(self, dev_pct: float) -> Signal:
        """穿越阈值 → 同向信号；未穿越 → SKIP。"""
        if dev_pct >= self.threshold_pct:
            return Signal(direction=Direction.UP, p_up=0.90)
        if dev_pct <= -self.threshold_pct:
            return Signal(direction=Direction.DOWN, p_up=0.10)
        return Signal(direction=Direction.SKIP, p_up=0.5)

    def _btc_contradicts(self, direction: Direction) -> bool:
        """BTC 反向穿越：标的 UP 但 BTC 已跌穿 -pct%，或标的 DOWN 但 BTC 已涨穿 +pct%。

        依据 docs/reports/btc_cross_asset_signal.md：标的穿越方向与 BTC 相反时
        命中率暴跌（30 天 41% vs 80%；昨晚实盘 5 笔 -3.09），是假穿越（标的
        独立噪音，非市场动量）。BTC 价默认 REST 拉取（~1s，反向状态非瞬时
        事件，延迟可接受）；每窗口懒加载 BTC 基准。
        """
        if self.btc_contradiction_pct <= 0:
            return False
        if self._btc_base is None:
            self._btc_base = self._fetch_symbol_window_open("BTC")
            if self._btc_base is None:
                return False
        price = self._fetch_btc_price()
        if price is None or self._btc_base <= 0:
            return False
        dev = (price - self._btc_base) / self._btc_base * 100.0
        if direction is Direction.UP and dev <= -self.btc_contradiction_pct:
            return True
        if direction is Direction.DOWN and dev >= self.btc_contradiction_pct:
            return True
        return False

    def _apply_btc_filter(self, signal: Signal) -> Signal:
        """BTC 反向矛盾过滤：穿越方向与 BTC 相反 → 降级 SKIP（不入场）。"""
        if signal.direction is Direction.SKIP:
            return signal
        if self._btc_contradicts(signal.direction):
            return Signal(direction=Direction.SKIP, p_up=0.5)
        return signal

    def generate_signal(self, context: SignalContext | None = None) -> Signal:
        """窗口开始：建基准价 + 立即检测一次穿越。

        新窗口（roll_window 重置 signal → lifecycle 重新调用本方法）：
        重建窗口基准，保证穿越检测锚定到当前窗口开盘价。
        """
        self._base = self._fetch_window_open()
        price = self._fetch_price()
        self._last_price = price
        if price is None or self._base is None:
            return Signal(direction=Direction.SKIP, p_up=0.5)
        dev = self._deviation_pct(price)
        if dev is None:
            return Signal(direction=Direction.SKIP, p_up=0.5)
        return self._apply_btc_filter(self._signal_for(dev))

    def refresh_signal(self, context: SignalContext | None = None) -> Signal | None:
        """窗口内每 tick：实时监控穿越。穿越返回同向信号，未穿越返回 None。

        None = 不刷新（保持当前信号），避免无意义更新刷屏。
        基准缺失时尝试重建（网络失败的恢复路径）。
        """
        if self._base is None:
            self._base = self._fetch_window_open()
        price = self._fetch_price()
        if price is not None:
            self._last_price = price
        dev = self._deviation_pct(price) if self._base is not None else None
        if dev is None:
            return None
        return self._apply_btc_filter(self._signal_for(dev))

    def status_text(self) -> str | None:
        """面板状态：窗口基准价 / 实时偏离 / 穿越阈值 / 穿越状态。"""
        if self._base is None:
            return f"策略状态: momentum（穿越±{self.threshold_pct}%） 基准 — 偏离 —"
        dev = (
            self._deviation_pct(self._last_price)
            if self._last_price is not None else None
        )
        dev_s = "—" if dev is None else f"{dev:+.3f}%"
        state = "⏳ 等待穿越"
        if dev is not None and dev >= self.threshold_pct:
            state = "🔺 已穿越 UP"
        elif dev is not None and dev <= -self.threshold_pct:
            state = "🔻 已穿越 DOWN"
        return (
            f"策略状态: momentum（穿越±{self.threshold_pct}%） "
            f"基准 {self._base:,.1f} 偏离 {dev_s} {state}"
        )

    # ---- 结算判定（dry-run 模拟结算单一事实源） ----

    def window_outcome(self, window_start: int) -> Direction | None:
        """窗口实际结算方向：窗口 K 的 close vs open（与回测口径一致）。

        目标窗口的 binance K（ts == window_start×1000）闭合后可得；
        K 未闭合/拉取失败返回 None，Settler 回退 gamma 流程。
        """
        try:
            since_ms = int(window_start) * 1000 - self.step_ms
            ks = fetch_klines_batch(
                self.symbol, self.interval, since=since_ms, limit=3, proxies=None
            )
        except Exception:
            return None
        for k in ks:
            if int(k.timestamp) == int(window_start) * 1000:
                o, c = float(k.open), float(k.close)
                if o <= 0:
                    return None
                return Direction.UP if c > o else Direction.DOWN
        return None

    def reset_runtime_data(self) -> None:
        """清空窗口基准（策略无持久化状态，仅内存锚点）。"""
        self._base = None
        self._btc_base = None