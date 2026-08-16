"""钱包外部世界同步：余额快照 + 实时持仓核对（幽灵持仓清除/未跟踪接管）。

引擎 tick 的"外部世界同步"关注点收敛于此（深模块）：余额定时刷新
（节流 + 今日盈亏基准捕获）与 Polymarket 实时持仓核对（防幽灵持仓）。
引擎只保留一行调用；核对规则可独立测试（注入窄替身，无需完整 tick 时序）。

依赖经 WalletSource 窄接口注入（执行器隐式实现，测试可注入 fake）；
state 由 reconcile 入参传入（引擎 reset 会重建 TradeState 对象，不持有引用）。
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from pmbot.types import Direction, Position

logger = logging.getLogger(__name__)

# 钱包余额刷新间隔（秒）：余额变化慢，低频查询避免无谓 RPC
BALANCE_REFRESH_SEC = 30
# 幽灵持仓判定宽限（秒）：持仓窗口结束后再过此宽限，Polymarket 仍无该持仓才判幽灵。
# 买入后 /positions 链上索引有延迟（数十秒），立即核对会误清真实持仓。
GHOST_GRACE_SEC = 180


class WalletSource(Protocol):
    """钱包能力窄接口：余额与实时持仓（执行器隐式实现，测试可注入 fake）。

    live_positions 返回 None 表示查询失败（调用方必须区分「无持仓」与
    「查询失败」——后者不核对，防误清真实持仓）。
    """

    def collateral_balance(self) -> float | None: ...
    def live_positions(self, user: str | None = None) -> list[dict] | None: ...


class WalletReconciler:
    """钱包核对器：按节流周期刷新余额快照与实时持仓（幽灵清除/未跟踪接管）。

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
        step_sec: int = 300,
    ):
        self._source = source
        self._save = save_status
        self._dry_run = dry_run
        self._refresh_sec = refresh_sec
        self._step_sec = step_sec  # 窗口步长（幽灵判定/接管计时用）
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
        - 本地有持仓但 Polymarket 无 → 疑似幽灵持仓（崩溃/强杀残留）：
          仅当持仓窗口已结束并超过宽限期（GHOST_GRACE_SEC）才清除——
          买入后 /positions 索引延迟数十秒，立即核对会误清真实持仓，
          使止损/时间止损/结算全部失效（回归事故：20:05:33 误清 20:05:30 买入持仓）；
        - 本地无但 Polymarket 有 → 未跟踪持仓（成交响应丢失/误清恢复）：
          slug 可解析窗口起点时自动接管重建本地持仓（止损/结算恢复管理），
          无法解析（非 bot 市场格式，疑似手动仓位）只警告不接管。
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
            # 本地有持仓、Polymarket 无本标的持仓 → 疑似幽灵持仓（已平仓/已结算但本地未清除）
            pos = state.position
            # 防误清竞态：刚买入的持仓 Polymarket /positions 索引有延迟（数十秒），
            # 立即核对会误判幽灵 → 真实持仓失去止损/时间止损/结算管理。
            # 仅当持仓窗口已结束并超过宽限期才判定真幽灵（崩溃/强杀残留必早于此）。
            if now_sec < pos.window_start + self._step_sec + GHOST_GRACE_SEC:
                logger.warning(
                    "本地持仓 %s %.4f 股（窗口 %d）暂未在 Polymarket 出现"
                    "（窗口未结束/索引延迟），跳过幽灵清除",
                    pos.direction.value, pos.size, pos.window_start,
                )
            else:
                logger.warning(
                    "幽灵持仓清除：本地记录 %s %.4f 股（窗口 %d），Polymarket 无实际持仓（%d 条实时持仓中无 %s）",
                    pos.direction.value, pos.size, pos.window_start,
                    len(positions), state.symbol,
                )
                state.position = None
                changed = True
        elif state.position is None and mine:
            # 本地无但 Polymarket 有 → 未跟踪持仓：自动接管（slug 可解析窗口起点时），
            # 恢复止损/结算管理；无法解析（非 bot 市场格式）只警告不接管（防误接管手动仓位）。
            if self._adopt_untracked(mine, state, now_sec):
                changed = True
        if changed:
            self._save()

    def _adopt_untracked(self, mine: list[dict], state, now_sec: int) -> bool:
        """从未跟踪持仓重建本地 position（接管）。

        场景：下单成交但响应丢失（实盘无成交数据放弃建仓）、误清恢复等——
        本地无记录但 Polymarket 有本标的持仓。不接管则持仓失去
        止损/时间止损/结算管理（资金裸奔，UI 与实盘不符）。

        仅接管 slug 可解析出窗口起点（bot 市场格式）的仓位：
        方向/股数/均价来自官方 /positions 实际数据；时间止损从接管时刻重新计时。
        返回是否接管。
        """
        for p in mine:
            try:
                window_start = int(str(p.get("slug") or "").rsplit("-", 1)[-1])
            except ValueError:
                continue  # slug 无窗口起点：非 bot 市场格式，不接管
            size = float(p.get("size") or 0)
            entry = float(p.get("avgPrice") or 0)
            if size <= 0 or entry <= 0:
                continue
            direction = Direction.UP if str(p.get("outcome", "")).lower() == "up" else Direction.DOWN
            state.position = Position(
                direction=direction,
                entry_price=entry,
                size=size,
                entered_remaining_sec=max(0, window_start + self._step_sec - now_sec),
                window_start=window_start,
            )
            # 同窗口已下过注：阻止接管后本窗口重复买入（窗口无法判断时保守置 True）
            if state.window_start is None or state.window_start == window_start:
                state.window_bet_placed = True
            logger.warning(
                "自动接管未跟踪持仓：%s %.4f 股 @ %.3f（窗口 %d，Polymarket /positions）。"
                "止损/结算已恢复管理；如非本 bot 仓位请人工核实",
                direction.value, size, entry, window_start,
            )
            return True
        logger.warning(
            "发现未跟踪持仓：Polymarket 有 %s 持仓（%d 条）但本地无记录，且无法解析窗口起点（slug 非 bot 市场格式），请人工核对",
            state.symbol, len(mine),
        )
        return False
