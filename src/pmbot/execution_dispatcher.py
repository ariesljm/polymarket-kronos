"""执行分派器：TradingLoop 的动作执行与挂单成交检测关注点。

从 TradingLoop 提取的深模块，将执行分派（execute/_exec_*）与挂单成交检测
（refresh_pending/_fill_pending）收敛于此，使 TradingLoop 退化为纯编排器。
"""

from __future__ import annotations

import logging

from pmbot.executor_protocols import MarketBook, TradeExecutor
from pmbot.market_discovery import MarketInfo
from pmbot.state import StateStore, TradeState
from pmbot.types import (
    Action,
    ActionType,
    Direction,
    Position,
    rebuilt_position,
    token_for,
)

logger = logging.getLogger(__name__)


class ExecutionDispatcher:
    """动作执行与挂单成交检测。

    接收 TradingLoop 的 state/io 依赖，提供 execute/refresh_pending 两个接缝方法
    （LifecycleDeps 消费），以及 close_position/abandon_position 供结算回调。
    """

    def __init__(
        self,
        *,
        state: TradeState,
        trade: TradeExecutor,
        book: MarketBook,
        store: StateStore,
        dry_run: bool,
        step_sec: int,
        save_status,
    ):
        self.state = state
        self.trade = trade
        self.book = book
        self._store = store
        self.dry_run = dry_run
        self.step_sec = step_sec
        self._save = save_status

    # ---- 接缝方法（LifecycleDeps 消费） ----

    def execute(self, action: Action, market: MarketInfo, now_sec: int) -> None:
        """动作分派：按类型路由到对应执行方法（薄分派器）。"""
        st = self.state
        if action.type is ActionType.PLACE_MARKET:
            self._exec_place_market(action, market, now_sec)
        elif action.type is ActionType.CANCEL:
            self._exec_cancel(action)
        elif action.type is ActionType.SELL:
            self._exec_sell(action, market)
        elif action.type is ActionType.PAUSE:
            self._exec_pause(action)

    def refresh_pending(self, market: MarketInfo, now_sec: int) -> None:
        """挂单成交检测（lifecycle tick 每 tick 调用）。

        调用方（TradingLoop）已处理 WS 连接跳过与 dry-run 分流，
        本方法只做盘口模拟成交（dry-run）或 REST 查询（live）。
        """
        st = self.state
        pending = st.pending_order
        if pending is None:
            return
        if self.dry_run:
            # dry-run 模拟成交：真实盘口 ask ≤ 限价即视为成交
            token = token_for(market, pending.direction)
            ask = self.book.best_ask(token)
            if ask is not None and ask <= float(pending.price):
                self.fill_pending(now_sec, entry_price=ask)
            return
        try:
            order = self.trade.get_order(pending.order_id)
        except Exception:
            logger.exception("查询挂单失败")
            return
        if isinstance(order, dict) and order.get("status") == "filled":
            self.fill_pending(now_sec)

    # ---- 执行分派 ----

    def _exec_place_market(self, action: Action, market: MarketInfo, now_sec: int) -> None:
        st = self.state
        if st.window_bet_placed:
            return
        token = token_for(market, action.direction)
        ask = self.book.best_ask(token)
        if ask is None:
            logger.warning("市价买入跳过：盘口无报价 %s", token[:16])
            return
        target_size = action.amount / ask
        filled = self.trade.market_buy(token, action.amount)
        if filled is None:
            logger.warning("市价买入失败/无成交数据：%s，下 tick 重试", token[:16])
            return
        entry = filled.avg_price or ask
        size = filled.filled_size or target_size
        st.position = rebuilt_position(
            action.direction, entry, size, st.window_start, now_sec, self.step_sec,
        )
        st.window_bet_placed = True
        self._save()
        src = "API" if filled.avg_price and filled.filled_size else "盘口估算"
        logger.info(
            "市价买入：%s %s %.4f 股 @ %.3f (成本 %.2f USDC, 数据源 %s)",
            token[:16], action.direction.value, size, entry, size * entry, src,
        )

    def _exec_cancel(self, action: Action) -> None:
        st = self.state
        if st.pending_order:
            self.trade.cancel(st.pending_order.order_id)
            logger.info("撤单：%s", st.pending_order.order_id)
            st.pending_order = None

    def _exec_sell(self, action: Action, market: MarketInfo) -> None:
        st = self.state
        pos = st.position
        if pos is None:
            return
        token = token_for(market, pos.direction)
        try:
            fill = self.trade.market_sell(token, pos.size)
        except Exception:
            logger.warning("市价卖出失败（%s %s %.4f 股），保留持仓等待下一 tick",
                           token[:16], pos.direction.value, pos.size)
            return
        if fill is None:
            logger.warning("市价卖出无成交（%s %s %.4f 股），保留持仓等待下一 tick",
                           token[:16], pos.direction.value, pos.size)
            return
        exit_price = fill.avg_price or 0.0
        proceeds = None
        if not self.dry_run and fill.order_id:
            proceeds = self.trade.sell_proceeds(fill.order_id, token)
        self.close_position(pos, exit_price, action.reason or "sell", proceeds=proceeds)

    def _exec_pause(self, action: Action) -> None:
        st = self.state
        st.paused = True
        st.pause_reason = {
            "consecutive_losses": "连亏熔断",
            "daily_loss": "日亏熔断",
        }.get(action.reason or "", "熔断暂停")
        logger.warning("收到暂停指令（%s）", st.pause_reason)

    # ---- 挂单成交 ----

    def fill_pending(self, now_sec: int, entry_price: float | None = None) -> None:
        st = self.state
        pending = st.pending_order
        entry = entry_price if entry_price is not None else float(pending.price)
        st.position = rebuilt_position(
            pending.direction, entry, float(pending.size), st.window_start, now_sec, self.step_sec,
        )
        st.pending_order = None
        logger.info("挂单成交：%s @ %.2f，建立持仓", pending.direction, entry)

    # ---- 平仓 ----

    def close_position(self, pos: Position, exit_price: float, reason: str,
                       proceeds: float | None = None) -> float:
        """平仓共用路径（卖出/结算）。"""
        st = self.state
        pnl = None
        if proceeds is not None:
            pnl = proceeds - pos.size * pos.entry_price
        result = st.close_position(exit_price, actual_pnl=pnl, pos=pos)
        self._store.log_trade(
            st,
            window_start=pos.window_start,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size,
            pnl=result,
            reason=reason,
        )
        logger.info("平仓（%s）：%s @ %.3f，盈亏 %.4f%s",
                    reason, pos.direction.value, exit_price, result,
                    "（真实兑付）" if (pnl is not None and reason == "settle")
                    else "（真实成交）" if pnl is not None else "（理论价差）")
        if reason == "settle":
            self._save()
        return result

    def abandon_position(self) -> None:
        """结算放弃持仓跟踪。"""
        st = self.state
        st.position = None
        st.settle_pending = None
        self._save()

    # ---- 盘口采样器订阅 ----

    def subscribe_sampler(self, market: MarketInfo, user_stream=None) -> None:
        """BookSampler 订阅当前窗口 token。"""
        sampler = self.book.sampler
        if sampler is not None:
            sampler.subscribe(
                [market.yes_token_id, market.no_token_id],
                direction_map={market.yes_token_id: "up", market.no_token_id: "down"},
            )
        if user_stream is not None:
            user_stream.subscribe_markets([market.condition_id])