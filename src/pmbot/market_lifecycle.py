"""MarketLifecycle：单个市场窗口的生命周期状态机。

参考 polymarket-trade-engine 的 EarlyBird + MarketLifecycle 架构：
INIT → RUNNING → STOPPING → DONE

- INIT: 信号生成（Kronos 推理），完成后进入 RUNNING
- RUNNING: 成交检测、持仓管理、决策执行（每 tick）
- STOPPING: 引擎在窗口切换/停机时调用——撤遗留挂单、结算待处理持仓
- DONE: 生命周期结束（对象可丢弃）

依赖通过 LifecycleDeps 窄接口注入（状态/策略/执行器/决策接缝），
不持有引擎整体引用——测试可注入 fake，无需构造完整 TradingLoop。

引擎（TradingLoop）负责：日界/熔断、窗口切换编排、市场发现、
跨窗口持仓结算兜底——这些是"引擎级"关注点，不随单窗口生命周期消亡。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Protocol

from pmbot.config import Config
from pmbot.market_discovery import MarketInfo
from pmbot.state import TradeState
from pmbot.strategy import Strategy
from pmbot.types import Action, Direction, MarketView, token_for

logger = logging.getLogger(__name__)


class Phase(Enum):
    INIT = "init"
    RUNNING = "running"
    STOPPING = "stopping"
    DONE = "done"


class CancelExecutor(Protocol):
    """执行器的最小能力：撤单（TradingLoop 隐式实现，测试可注入 fake）。"""

    def cancel(self, order_id: str) -> bool: ...


class LifecycleDeps(Protocol):
    """MarketLifecycle 需要的窄接口（TradingLoop 隐式实现，测试可注入 fake）。

    只暴露生命周期真正消费的能力：状态、信号源、执行器，以及
    成交检测/决策/执行/落盘四个接缝方法——不再穿透引擎私有成员。
    """

    state: TradeState
    strategy: Strategy
    executor: CancelExecutor
    config: Config
    step_sec: int  # 窗口步长（秒）

    def refresh_pending(self, market: MarketInfo, now_sec: int) -> None: ...
    def build_view(self, market: MarketInfo, now_sec: int) -> MarketView: ...
    def decide(self, view: MarketView) -> Action: ...
    def execute(self, action: Action, market: MarketInfo, now_sec: int) -> None: ...
    def save_status(self) -> None: ...


class MarketLifecycle:
    """单个市场窗口的生命周期。依赖经 LifecycleDeps 窄接口注入。"""

    def __init__(self, deps: LifecycleDeps, window_start: int, now_sec: int):
        self.deps = deps
        self.window_start = window_start
        self.phase = Phase.INIT
        self.created_sec = now_sec

    # ---- 状态迁移 ----

    def start(self, now_sec: int, market: MarketInfo) -> None:
        """INIT → RUNNING：生成窗口信号（若本窗口尚未生成）。"""
        st = self.deps.state
        if st.signal is None:
            remaining = self.window_start + self.deps.step_sec - now_sec
            if remaining <= self.deps.config.no_entry_before_end_sec:
                # 窗口末禁入：中途启动时剩余不足不推理，保持 INIT 等窗口切换
                logger.info("窗口 %d 剩余 %ds ≤ 禁入阈值 %ds，跳过推理等待下一窗口",
                            self.window_start, remaining, self.deps.config.no_entry_before_end_sec)
                return
            st.predicting = True
            st.predict_start_sec = now_sec
            self.deps.save_status()  # 推理开始即落盘（面板可实时显示）
            try:
                st.signal = self.deps.strategy.generate_signal({"now_ms": now_sec * 1000})
            finally:
                st.predicting = False
                st.last_predict_sec = now_sec
            logger.info("信号: %s (P(up)=%.3f)", st.signal.direction.value, st.signal.p_up)
        self.phase = Phase.RUNNING

    def tick(self, now_sec: int, market: MarketInfo) -> None:
        """RUNNING：每 tick 的窗口级逻辑（成交检测 → 决策 → 执行）。"""
        if self.phase is not Phase.RUNNING:
            return
        # 挂单成交检测
        self.deps.refresh_pending(market, now_sec)
        # 决策与执行
        view = self.deps.build_view(market, now_sec)
        action = self.deps.decide(view)
        self.deps.execute(action, market, now_sec)
        self.deps.save_status()

    def stop(self, now_sec: int) -> None:
        """STOPPING → DONE：撤遗留挂单（幂等），标记结束。

        引擎负责：跨窗口判定（state.window_start）、roll_window、撤单兜底。
        """
        if self.phase is Phase.DONE:
            return
        st = self.deps.state
        if st.pending_order is not None and st.pending_order.order_id:
            self.deps.executor.cancel(st.pending_order.order_id)
            logger.info("生命周期收尾撤单：%s", st.pending_order.order_id)
        self.phase = Phase.DONE

    # ---- 查询 ----

    @property
    def is_active(self) -> bool:
        return self.phase in (Phase.INIT, Phase.RUNNING, Phase.STOPPING)



