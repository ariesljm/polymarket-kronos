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
# Polymarket 价格区间 0.01~0.99（tick 0.01）：市价卖出“接受任意合法价”的显式保护价。
# 与不传保护价时 SDK 自动拉盘口算吃穿价的实际效果等价，但省掉一次 REST。
MIN_TICK_PRICE = 0.01


class SamplerProto(Protocol):
    """盘口采样器窄接口：取内存快照与新鲜度（BookSampler 隐式实现，可注入 None）。

    健康观测语义：快照新鲜度（is_fresh / snapshot_age）供健康检查与面板展示用；
    **决策路径不依赖它**——ClobExecutor._best_price 为性能采用独立长阈值
    MAX_BOOK_AGE_SEC（决策容忍陈旧避免同步 REST，见 clob_executor）。两套阈值
    语义不同、各有消费方，勿误以为 is_fresh 是决策价的新鲜度事实源（曾文档
    声称"消费方不自行实现"但决策路径实际绕过——此为有意取舍）。
    update_snapshot: 消费方 REST 现拉结果回填（防重复 REST）；
    subscribe: 订阅/退订 token（传 [] 退订；direction_map 为 token→方向映射）。
    """

    def snapshot(self, token_id: str) -> dict | None: ...
    def is_fresh(self, token_id: str) -> bool: ...
    def update_snapshot(self, token_id: str, book: dict) -> None: ...
    def connection_status(self) -> str: ...
    def subscribe(self, tokens: list[str], direction_map: dict[str, str] | None = None) -> None: ...


class MarketBook(Protocol):
    """盘口能力窄接口：报价与盘口采样器挂载（执行器隐式实现）。

    size: 可成交量股数——定价基准（决策价贴近实际下单/持仓量级，
    开仓小单传 1.0 用最优档，持仓平仓传持仓股数，默认 5 保守口径）。
    消费方：引擎报价（build_view / 决策 / 盘口展示）、盘口采样器构造。
    """

    def best_ask(self, token_id: str, size: float = 5.0) -> float | None: ...
    def best_bid(self, token_id: str, size: float = 5.0) -> float | None: ...
    def fetch_book(self, token_id: str, timeout: float = 6.0) -> dict: ...
    def attach_sampler(self, sampler: SamplerProto) -> None: ...

    @property
    def sampler(self) -> SamplerProto | None: ...


class TradeExecutor(Protocol):
    """下单能力窄接口：市价/限价/撤单/成交确认。

    market_buy / market_sell 的可选保护价（max_price / min_price）直接作为
    市价单的限价——不传时 SDK 自行拉盘口算吃穿价（对小单≈不设限，下单时
    盘口已极化则照极化价成交）；传了则越界即拒单，并省掉 SDK 的一次
    get_order_book REST。

    warmup: 预热下单客户端按 token 缓存的元数据（tick/fee/condition_id），
    把下单前的额外 REST 移到窗口订阅时。

    消费方：引擎执行动作；生命周期成交检测只依赖其撤单子面
    （CancelExecutor，见 market_lifecycle）。
    """

    def market_buy(self, token_id: str, amount: float, max_price: float | None = None) -> Fill | None: ...
    def market_sell(self, token_id: str, size: float, min_price: float | None = None) -> Fill | None: ...
    def warmup(self, token_ids: list[str]) -> None: ...
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


def shares_for_amount(amount: float, price: float) -> float:
    """按美元金额换算份额（amount / price；execution_dispatcher 目标份额与
    模拟成交 filled_size 共用——曾两处各自手抄同一公式）。"""
    return amount / price