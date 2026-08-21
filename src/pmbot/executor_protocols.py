"""执行器窄接口 Protocols：执行器组合面的四个能力面。

OrderPlacer 是 TradeExecutor + MarketBook + WalletView + AuthSource 的并集，
两个适配器（ClobExecutor 实盘 / SimExecutor 模拟）实现全组合。
消费方按窄接口标注依赖，新适配器只需实现被消费的面。
"""

from __future__ import annotations

from typing import Protocol

from pmbot.types import Fill

CLOB_HOST = "https://clob.polymarket.com"

# Polymarket 限价单规则（实测验证 2026-08-14）：
# - 最小 5 股（minimum_order_size）
# - 可成交单最小金额 $1（size × price >= 1.0）
MIN_ORDER_SIZE = 5
MIN_ORDER_AMOUNT = 1.0


class SamplerProto(Protocol):
    """盘口采样器窄接口：取内存快照（BookSampler 隐式实现，可注入 None）。"""

    def snapshot(self, token_id: str) -> dict | None: ...


class MarketBook(Protocol):
    """盘口能力窄接口：报价与盘口采样器挂载（执行器隐式实现）。

    消费方：引擎报价（build_view / 决策 / 盘口展示）、盘口采样器构造。
    """

    def best_ask(self, token_id: str) -> float | None: ...
    def best_bid(self, token_id: str) -> float | None: ...
    def fetch_book(self, token_id: str) -> dict: ...
    def attach_sampler(self, sampler: SamplerProto) -> None: ...
    def sampler(self) -> SamplerProto | None: ...


class TradeExecutor(Protocol):
    """下单能力窄接口：市价/限价/撤单/成交确认。

    消费方：引擎执行动作；生命周期成交检测只依赖其撤单子面
    （CancelExecutor，见 market_lifecycle）。
    """

    def market_buy(self, token_id: str, amount: float) -> Fill | None: ...
    def market_sell(self, token_id: str, size: float) -> Fill | None: ...
    def sell_proceeds(self, order_id: str, token_id: str) -> float | None: ...
    def settle_proceeds(self, condition_id: str) -> float | None: ...
    def place_limit(self, token_id: str, side: str, price: float, size: float) -> str | None: ...
    def sell(self, token_id: str, size: float, price: float) -> str | None: ...
    def cancel(self, order_id: str) -> bool: ...
    def get_order(self, order_id: str) -> dict | None: ...


class WalletView(Protocol):
    """钱包能力窄接口：余额与实时持仓（WalletReconciler / 面板消费）。

    live_positions 返回 None 表示查询失败（调用方必须区分「无持仓」与
    「查询失败」——后者不核对，防误清真实持仓）。
    """

    def collateral_balance(self) -> float | None: ...
    def live_positions(self, user: str | None = None) -> list[dict] | None: ...


class AuthSource(Protocol):
    """凭证窄接口：CLOB API 凭证（UserStream 认证用，与下单客户端同源）。"""

    def api_auth(self) -> dict | None: ...


class OrderPlacer(TradeExecutor, MarketBook, WalletView, AuthSource, Protocol):
    """执行器组合面：下单 + 盘口 + 钱包 + 凭证四个窄接口的并集。

    两个适配器（ClobExecutor 实盘 / SimExecutor 模拟）实现全组合；
    消费方按窄接口标注依赖（引擎内部按面使用），新适配器只需实现
    被消费的面。模拟适配器的盘口/钱包/凭证面委托内部实盘实例。
    """


def validate_limit_order(size: float, price: float) -> None:
    """Polymarket 限价单规则校验（单一事实源）：≥5 股且金额 ≥$1。"""
    if size < MIN_ORDER_SIZE or size * price < MIN_ORDER_AMOUNT:
        raise ValueError(
            f"订单不满足 Polymarket 规则：size={size} (最小 {MIN_ORDER_SIZE} 股)，"
            f"金额={size * price:.2f} (最小 ${MIN_ORDER_AMOUNT})"
        )


def min_shares_for_price(price: float) -> float:
    """满足 Polymarket 订单规则的最小股数：≥5 股且金额 ≥$1。"""
    import math

    return max(MIN_ORDER_SIZE, math.ceil(MIN_ORDER_AMOUNT / price))