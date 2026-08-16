"""CLOB 执行器：Polymarket CLOB 下单/撤单/卖出/盘口查询。

基于 py-clob-client-v2。L1 认证：钱包私钥派生 API key（缓存本地）；
L2 认证：api creds。dry_run 模式只打印指令，不碰真实订单。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

from pmbot.book_price import weighted_price

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"

# Polymarket 限价单规则（实测验证 2026-08-14）：
# - 最小 5 股（minimum_order_size）
# - 可成交单最小金额 $1（size × price >= 1.0）
MIN_ORDER_SIZE = 5
MIN_ORDER_AMOUNT = 1.0


class SamplerProto(Protocol):
    """盘口采样器窄接口：取内存快照（BookSampler 隐式实现，可注入 None）。"""

    def snapshot(self, token_id: str) -> dict | None: ...


def min_shares_for_price(price: float) -> float:
    """满足 Polymarket 订单规则的最小股数：≥5 股且金额 ≥$1。"""
    import math

    return max(MIN_ORDER_SIZE, math.ceil(MIN_ORDER_AMOUNT / price))


class ClobExecutor:
    def __init__(
        self,
        private_key: str | None = None,
        dry_run: bool = False,
        creds_cache: str = "data/clob_creds.json",
        chain_id: int = 137,  # Polygon 主网；Amoy 测试网 80002
        proxy_wallet: str | None = None,
        sampler: SamplerProto | None = None,
    ):
        """proxy_wallet: Polymarket 代理钱包地址（funder）。

        新架构（CTF Exchange V2 + pUSD）下 CLOB 订单使用 POLY_PROXY 签名
        （signature_type=2），funder 为代理钱包；资金为链上 pUSD。
        """
        # 私钥从 .env 读取（python-dotenv，幂等）
        from dotenv import load_dotenv

        load_dotenv()
        self._pk = private_key or os.environ.get("PRIVATE_KEY")
        self._proxy_wallet = proxy_wallet or os.environ.get("PROXY_WALLET")
        self._dry_run = dry_run
        self._chain_id = chain_id
        self._creds_cache = Path(creds_cache)
        self._client = None
        self._l1_client = None
        self._book_client = None
        self._sampler = sampler

    def _sampler_snapshot(self, token_id: str) -> dict | None:
        return self._sampler.snapshot(token_id) if self._sampler else None

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def attach_sampler(self, sampler: SamplerProto) -> None:
        """挂载 BookSampler（盘口快照优先读内存，REST 兜底）。"""
        self._sampler = sampler

    def fetch_book(self, token_id: str) -> dict:
        """公开盘口查询（无需认证；BookSampler REST 兜底用）。"""
        from py_clob_client_v2 import ClobClient

        if self._book_client is None:
            self._book_client = ClobClient(host=CLOB_HOST, chain_id=self._chain_id)
        return self._book_client.get_order_book(token_id)

    def api_auth(self) -> dict | None:
        """CLOB API 凭证（UserStream 认证用）：缓存完整则返回 auth dict，否则 None。

        与下单客户端同源（clob_creds.json），避免调用方重复解析。
        """
        creds = self._load_creds()
        if creds is None:
            return None
        return {
            "apiKey": creds.api_key,
            "secret": creds.api_secret,
            "passphrase": creds.api_passphrase,
        }

    def _get_l1(self):
        if self._l1_client is None:
            if not self._pk:
                raise RuntimeError("缺少钱包私钥：请在 .env 中配置 PRIVATE_KEY")
            from py_clob_client_v2 import ClobClient

            self._l1_client = ClobClient(host=CLOB_HOST, chain_id=self._chain_id, key=self._pk)
        return self._l1_client

    def _get_client(self):
        if self._client is None:
            from py_clob_client_v2 import ApiCreds, ClobClient

            creds = self._load_creds()
            if creds is None:
                creds = self._get_l1().create_or_derive_api_key()
                self._save_creds(creds)
            if not self._proxy_wallet:
                raise RuntimeError("缺少代理钱包地址：请在 .env 中配置 PROXY_WALLET")
            self._client = ClobClient(
                host=CLOB_HOST,
                chain_id=self._chain_id,
                key=self._pk,
                creds=creds,
                funder=self._proxy_wallet,
                signature_type=2,  # POLY_PROXY：代理钱包签名
            )
        return self._client

    def collateral_balance(self) -> float | None:
        """代理钱包的 pUSD 抵押余额（最小单位 1e6）。"""
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        r = self._get_client().get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if isinstance(r, dict) and r.get("balance") is not None:
            return int(r["balance"]) / 1e6
        return None

    def _load_creds(self) -> ApiCreds | None:
        """从本地缓存读取 ApiCreds（避免每次重新派生/400 噪音）。"""
        from py_clob_client_v2 import ApiCreds

        if not self._creds_cache.is_file():
            return None
        import json

        try:
            data = json.loads(self._creds_cache.read_text(encoding="utf-8"))
            return ApiCreds(
                api_key=data["api_key"],
                api_secret=data["api_secret"],
                api_passphrase=data["api_passphrase"],
            )
        except (KeyError, ValueError, json.JSONDecodeError):
            return None

    def _save_creds(self, creds: ApiCreds) -> None:
        import json

        self._creds_cache.parent.mkdir(parents=True, exist_ok=True)
        self._creds_cache.write_text(
            json.dumps(
                {
                    "api_key": creds.api_key,
                    "api_secret": creds.api_secret,
                    "api_passphrase": creds.api_passphrase,
                }
            ),
            encoding="utf-8",
        )

    def place_limit(self, token_id: str, side: str, price: float, size: float) -> str | None:
        """挂限价单。side: 'buy'/'sell'。返回 order_id（dry-run 返回模拟 id）。"""
        if size < MIN_ORDER_SIZE or size * price < MIN_ORDER_AMOUNT:
            raise ValueError(
                f"订单不满足 Polymarket 规则：size={size} (最小 {MIN_ORDER_SIZE} 股)，"
                f"金额={size * price:.2f} (最小 ${MIN_ORDER_AMOUNT})"
            )
        if self._dry_run:
            print(f"[dry-run] 挂单 {side.upper()} {size} @ {price} token={token_id[:16]}...")
            return f"dry-run-{token_id[:8]}"
        if side.lower() not in ("buy", "sell"):
            raise ValueError(f"side 必须是 buy/sell，实际: {side!r}")
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

        side_enum = Side.BUY if side.lower() == "buy" else Side.SELL
        resp = self._get_client().create_and_post_order(
            order_args=OrderArgs(token_id=token_id, price=price, side=side_enum, size=size),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.GTC,
        )
        if isinstance(resp, dict):
            return resp.get("orderID") or resp.get("order_id")
        return None

    def sell(self, token_id: str, size: float, price: float) -> str | None:
        """限价卖出持仓（止盈/止损）。"""
        return self.place_limit(token_id, "sell", price, size)

    def market_buy(self, token_id: str, size: float) -> str | None:
        """市价买入（FOK，按份额立即成交，可小数；无 5 股限制）。返回 order_id。"""
        if self._dry_run:
            print(f"[dry-run] 市价买 {size:.4f} 股 token={token_id[:16]}...")
            return f"dry-run-{token_id[:8]}"
        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side

        resp = self._get_client().create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=token_id,
                amount=size,  # BUY: 股数
                side=Side.BUY,
                order_type=OrderType.FOK,
            ),
            options=PartialCreateOrderOptions(),
        )
        if not resp:
            logger.warning("市价买入失败：无响应 token=%s", token_id[:16])
            return None
        logger.info("市价买入：%s %.4f 股，订单 %s", token_id[:16], size, resp)
        return resp

    def market_sell(self, token_id: str, size: float) -> float | None:
        """市价卖出持仓（FOK），返回成交价近似；dry-run 返回 best_bid。"""
        if self._dry_run:
            print(f"[dry-run] 市价卖 {size:.4f} 股 token={token_id[:16]}...")
            return self.best_bid(token_id)
        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side

        resp = self._get_client().create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=token_id,
                amount=size,  # SELL: 股数
                side=Side.SELL,
                order_type=OrderType.FOK,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
        # 尽力取成交均价；取不到由调用方回退 best_bid
        if isinstance(resp, dict):
            price = resp.get("average_price") or resp.get("avg_price") or resp.get("price")
            if price is not None:
                try:
                    return float(price)
                except (TypeError, ValueError):
                    pass
        return None

    def get_order(self, order_id: str) -> dict | None:
        """查询订单状态（成交检测）。"""
        return self._get_client().get_order(order_id)

    def cancel(self, order_id: str) -> bool:
        if self._dry_run:
            print(f"[dry-run] 撤单 {order_id}")
            return True
        if order_id.startswith("dry-run-"):
            return True
        try:
            from py_clob_client_v2 import OrderPayload

            resp = self._get_client().cancel_order(OrderPayload(orderID=order_id))
        except Exception:
            return False
        # 失败响应通常含 error 字段；异常已捕获，这里防误报
        if isinstance(resp, dict) and "error" in resp:
            return False
        return bool(resp)

    def best_bid(self, token_id: str) -> float | None:
        """持仓卖出可执行价（按 5 股可成交量加权，免疫垃圾挂单）。

        优先读 BookSampler 内存快照（高频采样），无快照时回退 REST 查询。
        """
        book = self._sampler_snapshot(token_id)
        if book is None:
            try:
                book = self.fetch_book(token_id)
            except Exception:
                return None
        return weighted_price(book, "bids", size=5)

    def best_ask(self, token_id: str) -> float | None:
        """买入可执行价（按 5 股可成交量加权，免疫垃圾挂单）。

        优先读 BookSampler 内存快照（高频采样），无快照时回退 REST 查询。
        """
        book = self._sampler_snapshot(token_id)
        if book is None:
            try:
                book = self.fetch_book(token_id)
            except Exception:
                return None
        return weighted_price(book, "asks", size=5)
