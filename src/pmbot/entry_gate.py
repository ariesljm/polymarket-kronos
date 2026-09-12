"""入场价闸门：入场价区间 [下限, 上限] 的判定单一事实源。

engine.decide（决策初判）与 ExecutionDispatcher（执行层二次校验）两处消费同一
接口：决策与下单之间盘口被 WS 线程异步更新（穿越后秒级极化），两处都须按同一
区间判定——曾各自实现上限比较（engine 另含下限、执行层无下限），
`override if override else config` 的回落也散在两处。

闸门无 IO 无状态：纯函数即接口即测试面，注入 ask 直接测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmbot.engine import AutoTuneOverride


@dataclass(frozen=True)
class EntryGate:
    """有效入场价区间（None = 该侧不限制）。

    上限：盘口 ask 高于此价不入场（穿越后追高仓位历史净亏，高价位 q−p 转负）。
    下限：盘口 ask 低于此价不入场（0.45-0.55 五五开档无信息优势，买卖价差双向吞噬）。

    注意 None 与 0.0 语义不同，勿混用：
    - None = 该侧不限制（config 的 `0 = 关闭` 由 resolve 映射而来）
    - 0.0 = 有效上限/下限 0（auto_tune 判「最低价带累计 EV≤0」→ 拒绝一切入场）
    曾以 `max_price > 0` 判定，把 auto_tune 的 0.00 上限当成「不限制」：盘口 0.98
    的仓位静默放行并以市价成交（2026-09-12 实盘事故）。
    """

    min_price: float | None = None
    max_price: float | None = None

    def check(self, ask: float | None) -> str | None:
        """入场价越界原因：entry_price_cap / entry_price_floor；放行返回 None。

        ask=None（无报价）不拦：缺报价不是价格越界，执行层缺报价本就放弃建仓。
        """
        if ask is None:
            return None
        if self.max_price is not None and ask > self.max_price:
            return "entry_price_cap"
        if self.min_price is not None and ask < self.min_price:
            return "entry_price_floor"
        return None


def resolve(config_min: float, config_max: float,
            override: "AutoTuneOverride | None" = None) -> EntryGate:
    """有效入场价区间：auto_tune 上限覆盖（override.max_entry_price 非 None 才
    覆盖；回落收口于此，消费方不自行 if/else）。

    下限无自适应，直取 config（config.max_entry_price 与 min_entry_price 的
    `min < max` 关系已在 config 校验）。

    config 的 `0 = 关闭` 映射为 None（不限制）；但 auto_tune 覆盖值 0.0 语义是
    「拒绝一切」而非「关闭」——它是收窄结果，不得回退 config。
    """
    cap = override.max_entry_price if override is not None else None
    max_price = (config_max or None) if cap is None else cap
    return EntryGate(min_price=config_min or None, max_price=max_price)
