"""钱包外部世界同步：余额快照 + 实时持仓核对（幽灵持仓清除）。

引擎 tick 的"外部世界同步"关注点收敛于此（深模块）：余额定时刷新
（节流 + 今日盈亏基准捕获）与 Polymarket 实时持仓核对（防幽灵持仓）。
引擎只保留一行调用；核对规则可独立测试（注入窄替身，无需完整 tick 时序）。

依赖经 WalletSource 窄接口注入（执行器隐式实现，测试可注入 fake）；
state 由 reconcile 入参传入（引擎 reset 会重建 TradeState 对象，不持有引用）。
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

# 钱包余额刷新间隔（秒）：余额变化慢，低频查询避免无谓 RPC
BALANCE_REFRESH_SEC = 30


class WalletSource(Protocol):
    """钱包能力窄接口：余额与实时持仓（执行器隐式实现，测试可注入 fake）。

    live_positions 返回 None 表示查询失败（调用方必须区分「无持仓」与
    「查询失败」——后者不核对，防误清真实持仓）。
    """

    def collateral_balance(self) -> float | None: ...
    def live_positions(self, user: str | None = None) -> list[dict] | None: ...


class WalletReconciler:
    """钱包核对器：按节流周期刷新余额快照与实时持仓（幽灵持仓清除）。

    - save_status 回调由引擎注入（changed 才落盘）；
    - dry_run 由引擎构造时注入（模拟持仓与真实钱包无关，不核对）；
    - state 每次 reconcile 传入：引擎 reset 重建 TradeState 后自动跟随。
    """

    def __init__(
        self,
        source: WalletSource,
        save_status: Callable[[], None],
        dry_run: bool = True,
        refresh_sec: int = BALANCE_REFRESH_SEC,
    ):
        self._source = source
        self._save = save_status
        self._dry_run = dry_run
        self._refresh_sec = refresh_sec
        self._last_balance_sec = 0
        self._last_positions_sec = 0

    def reconcile(self, now_sec: int, state) -> None:
        """按节流周期执行余额刷新与持仓核对（引擎每 tick 调用）。"""
        self._refresh_balance(now_sec, state)
        self._refresh_positions(now_sec, state)

    # ---- 余额 ----

    def _refresh_balance(self, now_sec: int, state) -> None:
        """定时刷新钱包余额快照（面板展示）；查询失败保留旧值。

        今日盈亏基准捕获：day_start_balance 为空（首日/跨天后）时，
        把当前余额记为今日起始基准，之后 今日盈亏 = 现余额 − 基准。
        """
        if now_sec - self._last_balance_sec < self._refresh_sec:
            return
        self._last_balance_sec = now_sec
        b = self._query_balance()
        if b is None:
            return
        changed = b != state.balance
        state.balance = b
        # 今日盈亏基准：仅实盘捕获（dry-run 钱包不变，今日盈亏继续用交易聚合）
        if state.day_start_balance is None and not self._dry_run:
            state.day_start_balance = b
            changed = True
        if changed:
            self._save()

    def _query_balance(self) -> float | None:
        """查询钱包余额；无凭证/网络失败返回 None（静默，不刷屏）。"""
        try:
            return self._source.collateral_balance()
        except Exception:
            return None

    # ---- 实时持仓核对 ----

    def _refresh_positions(self, now_sec: int, state) -> None:
        """Polymarket 实时持仓核对（官方 /positions）：防幽灵持仓 + UI 实时展示。

        每 30s 拉取钱包实时持仓，与本地 position 比对：
        - 本地有持仓但 Polymarket 无 → 幽灵持仓（崩溃/强杀残留），清除并警告；
        - 本地无但 Polymarket 有 → 未跟踪持仓，警告提示（不自动接管，防误操作）。
        快照写入 state.live_positions 供 UI 展示（实时持仓/现价/浮动盈亏）。
        """
        if now_sec - self._last_positions_sec < self._refresh_sec:
            return
        self._last_positions_sec = now_sec
        if self._dry_run:
            return  # 模拟持仓与真实钱包无关，不核对不展示
        positions = self._source.live_positions()
        if positions is None:
            return  # 查询失败：不核对不动本地（防误清真实持仓）
        # 实时持仓中是否有本标的（ETH/BTC...）的持仓：title 匹配符号或其全名
        aliases = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "bnb"}
        want = [state.symbol.lower(), aliases.get(state.symbol.lower(), "")]
        mine = [p for p in positions
                if any(w and w in str(p.get("title", "")).lower() for w in want)]
        changed = bool(state.live_positions != positions)
        state.live_positions = positions
        if state.position is not None and not mine:
            # 本地有持仓、Polymarket 无本标的持仓 → 幽灵持仓（已平仓/已结算但本地未清除）
            logger.warning(
                "幽灵持仓清除：本地记录 %s %.4f 股（窗口 %d），Polymarket 无实际持仓（%d 条实时持仓中无 %s）",
                state.position.direction.value, state.position.size, state.position.window_start,
                len(positions), state.symbol,
            )
            state.position = None
            changed = True
        elif state.position is None and mine:
            logger.warning(
                "发现未跟踪持仓：Polymarket 有 %s 持仓（%d 条）但本地无记录，请人工核对",
                state.symbol, len(mine),
            )
        if changed:
            self._save()
