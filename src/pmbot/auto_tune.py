"""分价格带自适应调参：按真实交易 EV 自动收窄入场价上限 / 调止盈。

用户要求"动态调 max_entry_price 和止盈策略,自动调整"。设计原则:
- 数据驱动 + 门槛保护:每价格带样本 ≥ min_band_n 才采信,小样本不动参数
  (噪声会越调越差;52 笔首日数据 0-0.65 全正、0.8+ 全负的结论只在样本足够时生效)
- 只收窄不放宽:默认 max_entry 0.65 有研究依据,仅在数据证明"某带真实亏损"
  时向下收窄;正向数据不主动放宽(避免小样本追高)
- 一切调整写日志 + status 可复盘,随时可关(override 字段全 None 即退化为纯 config)

用法: main_loop 每 60s 读 ledger → band_stats(trades) → tune() 生成 override(delta)
     → 注入 engine.decide(override.max_entry_price 覆盖入场价上限 / take_profit 覆盖止盈)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pmbot.engine import AutoTuneOverride

# 价格带（研究分档；0-0.65 首日全正 EV、0.8+ 负）
BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 0.3), (0.3, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 0.95), (0.95, 1.01),
)
# 止盈侧:低价带(≤0.45)胜率显著 > 55% 时视为"持有到结算近似最优",上调止盈接近持有
_LOW_BAND = (0.0, 0.45)


@dataclass(frozen=True)
class BandStat:
    """单价格带汇总（n 不足门槛时仅作参考，不参与调参）。"""

    n: int
    pnl: float
    wins: int

    @property
    def ev(self) -> float:
        """每笔期望收益（USDC/笔）。"""
        return self.pnl / self.n if self.n else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0


def _band_of(entry_price: float, lo: float, hi: float) -> bool:
    return lo <= entry_price < hi


def band_stats(trades: Iterable, bands: tuple[tuple[float, float], ...] = BANDS) -> dict[tuple[float, float], BandStat]:
    """按入场价带汇总真实交易（skips: entry_price 缺失/越界 忽略）。"""
    stats: dict[tuple[float, float], list] = {b: [0, 0.0, 0] for b in bands}  # n, pnl, wins
    for t in trades:
        p = getattr(t, "entry_price", None)
        if p is None:
            continue
        for b in bands:
            if _band_of(p, *b):
                n, pnl, wins = stats[b]
                n += 1
                pnl += t.pnl
                wins += 1 if t.pnl > 0 else 0
                stats[b] = [n, pnl, wins]
                break
    return {b: BandStat(*v) for b, v in stats.items()}


def tune(
    trades: Iterable,
    *,
    min_band_n: int = 10,
    config_max_entry: float = 0.65,
    config_take_profit: float = 0.95,
    bands: tuple[tuple[float, float], ...] = BANDS,
) -> AutoTuneOverride:
    """由真实交易统计生成引擎参数覆盖 delta（无足够证据返回空 delta = 维持 config）。

    max_entry_price: 从低到高按带累计 EV,首个「样本足且累计 EV ≤ 0」的带的
    下界作为新上限（收窄,不出现在 config 之上）；无调整 → None。
    take_profit: 低价带(≤0.45)样本足且胜率 > 55% → 上调到 0.99（接近持有到结算,
    研究:低带持到结算 EV +2.83 vs 早止盈 +0.29）；无严格上调 → None。
    """
    stats = band_stats(trades, bands)
    total_n = sum(s.n for s in stats.values())
    if total_n < min_band_n:
        return AutoTuneOverride()  # 全局样本不足：不覆盖任何参数（delta 空）

    # ---- max_entry: 累计 EV 扫描 ----
    new_max: float | None = None
    cum_pnl = 0.0
    for lo, hi in bands:
        s = stats[(lo, hi)]
        cum_pnl += s.pnl
        if s.n >= min_band_n and cum_pnl <= 0.0:
            new_max = lo  # 该带下界起进入负 EV 区间 → 收窄上限
            break
    if new_max is not None:
        # 只收窄不改宽：收窄结果不低于 config = 无有效覆盖（delta 空，不产生无效覆盖）
        new_max = new_max if new_max < config_max_entry else None

    # ---- take_profit: 低价带胜率 ----
    low = BandStat(0, 0.0, 0)
    for b, s in stats.items():
        if b[0] >= _LOW_BAND[0] and b[1] <= _LOW_BAND[1]:
            low = BandStat(low.n + s.n, low.pnl + s.pnl, low.wins + s.wins)
    new_tp: float | None = None
    if low.n >= min_band_n and low.win_rate > 0.55 and 0.99 > config_take_profit:
        new_tp = 0.99  # 低带胜率显著且严格上调 → 更接近持有（0.99 近似持有到结算）

    # delta：未调整字段留 None（回落由消费方解析层完成，不在 tune 内填充 config）
    return AutoTuneOverride(max_entry_price=new_max, take_profit=new_tp)


def tune_reason(override: AutoTuneOverride, stats: dict[tuple[float, float], BandStat]) -> str:
    """人类可读的调参原因（日志/复盘用）。override 为 delta：字段非 None 即本次调整。"""
    parts = []
    if override.max_entry_price is not None:
        parts.append(f"max_entry {override.max_entry_price:.2f}(带 EV≤0)")
    if override.take_profit is not None:
        parts.append(f"take_profit {override.take_profit:.2f}(低带胜率高)")
    if not parts:
        parts.append("无调整(样本不足或带 EV 均正)")
    detail = " ".join(f"{lo:.2f}:n{s.n}/ev{s.ev:+.2f}/wr{s.win_rate:.0%}" for (lo, hi), s in sorted(stats.items()))
    return " | ".join(parts) + " | bands: " + detail