"""CLOB 执行器：Polymarket CLOB 下单/撤单/卖出/盘口查询。

基于 py-clob-client-v2。L1 认证：钱包私钥派生 API key（缓存本地）；
L2 认证：api creds。dry_run 模式只打印指令，不碰真实订单。

窄接口定义见 executor_protocols.py；两个适配器（ClobExecutor 实盘 / SimExecutor 模拟）
实现 OrderPlacer 组合面。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pmbot.book_price import weighted_price
from pmbot.executor_protocols import (
    CLOB_HOST,
    Fill,
    SamplerProto,
    validate_limit_order,
)

logger = logging.getLogger(__name__)


class ClobExecutor:
    """实盘执行器：Polymarket CLOB 下单/撤单/卖出/盘口/余额。"""

    def __init__(
        self,
        private_key: str | None = None,
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
        self._chain_id = chain_id
        self._creds_cache = Path(creds_cache)
        self._client = None
        self._l1_client = None
        self._book_client = None
        self._sampler = sampler

    def _sampler_snapshot(self, token_id: str) -> dict | None:
        return self._sampler.snapshot(token_id) if self._sampler else None

    @property
    def sampler(self) -> SamplerProto | None:
        """当前采样器引用（主循环订阅市场用；无采样器返回 None）。"""
        return self._sampler

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

    def live_positions(self, user: str | None = None) -> list[dict] | None:
        """Polymarket 实时持仓（官方 data-api /positions，user 过滤有效）。

        返回 [{asset, conditionId, size, avgPrice, curPrice, currentValue,
        cashPnl, realizedPnl, redeemable, title, outcome, ...}]；
        查询失败（网络/无地址）返回 None——调用方必须区分「无持仓」与「查询失败」。
        user 缺省用代理钱包地址（.env PROXY_WALLET）。
        """
        import requests

        addr = user or self._proxy_wallet
        if not addr:
            return None
        try:
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": addr, "limit": 100},
                proxies={"https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10808")},
                timeout=30,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            return data if isinstance(data, list) else None
        except Exception:
            return None

    def settle_proceeds(self, condition_id: str) -> float | None:
        """结算兑付真实到账（data-api /activity REDEEM）：匹配 conditionId 的 REDEEM 记录 usdcSize。
        市场结算后赢的持仓自动兑付为 USDC（链上 REDEEM 交易），usdcSize 为实际到账
        金额（含本金）；结算记账用 到账 − 成本 替代理论价差，口径与钱包一致。
        查询失败/无匹配记录返回 None——调用方回退理论价差（与 sell_proceeds 同模式）。
        """
        import requests

        addr = self._proxy_wallet
        if not addr or not condition_id:
            return None
        try:
            r = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"user": addr, "type": "REDEEM", "limit": 200},
                proxies={"https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10808")},
                timeout=30,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if not isinstance(data, list):
                return None
            want = str(condition_id).lower()
            for a in data:
                if str(a.get("conditionId") or "").lower() == want:
                    usdc = a.get("usdcSize")
                    if usdc is not None:
                        return float(usdc)
            return None  # 有响应但该市场尚未 REDEEM（结算延迟/未兑付）
        except Exception:
            return None

    @property
    def wallet_address(self) -> str | None:
        """代理钱包地址（交易历史同步用）。"""
        return self._proxy_wallet

    def fetch_trade_page(self, offset: int = 0, limit: int = 500) -> list[dict]:
        """成交流水分页（data-api /activity?type=TRADE，倒序最新在前）；失败返回 []。

        用 /activity 而非 /trades：前者带 usdcSize（含手续费的美元金额），
        统计/报表切 API 口径需要它。失败返回 []（同步器静默跳过）。
        """
        import requests

        addr = self._proxy_wallet
        if not addr:
            return []
        try:
            r = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"user": addr, "type": "TRADE", "limit": limit, "offset": offset},
                proxies={"https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10808")},
                timeout=30,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def fetch_redeem_page(self, offset: int = 0, limit: int = 500) -> list[dict]:
        """结算兑付分页（data-api /activity?type=REDEEM，倒序最新在前）；失败返回 []。"""
        import requests

        addr = self._proxy_wallet
        if not addr:
            return []
        try:
            r = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"user": addr, "type": "REDEEM", "limit": limit, "offset": offset},
                proxies={"https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10808")},
                timeout=30,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

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
        """挂限价单。side: 'buy'/'sell'。返回 order_id。"""
        validate_limit_order(size, price)
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

    def market_buy(self, token_id: str, amount: float) -> Fill | None:
        """市价买入（FOK）。amount 为美元金额（SDK 语义：BUY=$$$）。

        返回 Fill（order_id/avg_price/filled_size）：优先取订单响应的
        averagePrice/matchedAmount，缺省时用 get_order 补查（以 API 为准，
        不靠本地盘口估算）。下单失败，或下单成功但 API 未返回实际成交数据
        （avg/size 缺）→ 返回 None：放弃建仓追踪，资金由 Polymarket 结算自动
        兑付（回归：盘口估算曾导致持仓股数记错 1.9231 vs 实际 1.7544）。
        """
        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side

        resp = self._get_client().create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=token_id,
                amount=amount,  # BUY: 美元金额（SDK 语义）
                side=Side.BUY,
                order_type=OrderType.FOK,
            ),
            options=PartialCreateOrderOptions(),
        )
        if not resp:
            logger.warning("市价买入失败：无响应 token=%s", token_id[:16])
            return None
        logger.info("市价买入响应：%s", resp)
        fill = self._parse_fill(resp, side="buy")
        if fill.avg_price is None or fill.filled_size is None:
            # 订单已成交但 API 未返回实际成交数据：放弃建仓追踪（防假持仓）。
            logger.warning("市价买入成交但缺实际成交数据（avg=%s size=%s），放弃建仓追踪",
                           fill.avg_price, fill.filled_size)
            return None
        return fill

    def _parse_fill(self, resp: dict, side: str = "buy") -> Fill:
        """从订单响应/详情提取真实成交（均价/份额）；缺失字段为 None。

        优先订单详情（get_order：price/size_matched 为服务端实际成交）；
        其次响应键（averagePrice/matchedAmount/takingAmount）。不反推估算。
        side: "buy"/"sell"（making/taking 方向随买卖互换）。
        """
        oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        avg = resp.get("averagePrice") or resp.get("avg_price") or resp.get("average_price") or resp.get("price")
        if side == "sell":
            # 卖单：makingAmount=卖出的 token 股数，takingAmount=收到的 USDC
            filled = (resp.get("matchedAmount") or resp.get("matched_amount")
                      or resp.get("size_matched") or resp.get("makingAmount"))
        else:
            # 买单：makingAmount=付出的 USDC，takingAmount=得到的 token 股数
            filled = (resp.get("matchedAmount") or resp.get("matched_amount")
                      or resp.get("size_matched") or resp.get("takingAmount"))
        # 市价单响应含 making/taking 金额：实际成交价 = 付/得（订单 price 是保护价非成交价）
        if avg is None:
            making, taking = resp.get("makingAmount"), resp.get("takingAmount")
            if making is not None and taking is not None:
                try:
                    m, t = float(making), float(taking)
                    if m > 0 and t > 0:
                        # 买单：价 = making/taking（付出的 USDC/得到的 token）；
                        # 卖单：价 = taking/making（收到的 USDC/卖出的 token）。
                        # 回归：20:39 卖单方向反算 → exit_price=3.571（1/0.28）写入 trades.csv。
                        avg = (m / t) if side == "buy" else (t / m)
                except (TypeError, ValueError):
                    pass
        if (avg is None or filled is None) and oid:
            try:  # 响应缺成交字段 → 订单详情补查（price/size_matched 为实际成交）
                detail = self.get_order(oid)
                if isinstance(detail, dict):
                    avg = avg or detail.get("averagePrice") or detail.get("avg_price") \
                          or detail.get("average_price") or detail.get("price")
                    filled = filled or (detail.get("matchedAmount") or detail.get("matched_amount")
                                        or detail.get("size_matched") or detail.get("takingAmount"))
            except Exception:
                pass
        def _f(x):
            try:
                return float(x) if x is not None else None
            except (TypeError, ValueError):
                return None

        return Fill(order_id=oid, avg_price=_f(avg), filled_size=_f(filled))

    def market_sell(self, token_id: str, size: float) -> Fill | None:
        """市价卖出持仓（FOK），返回 Fill（order_id/avg_price）。

        avg_price 为成交均价近似（响应字段 → 订单详情 price，卖单 making/taking
        方向反算）；取不到时回退 best_bid（0.0 为最后防御）。order_id 供
        sell_proceeds 聚合真实到账。成交解析与买入共用 _parse_fill（单一事实源）。
        """
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
        if not isinstance(resp, dict):
            return None
        fill = self._parse_fill(resp, side="sell")  # 卖单 making/taking 方向与买单相反
        if fill.avg_price is None:
            # 价格回退在成交语义内（调用方不再各自 best_bid）
            fill = Fill(order_id=fill.order_id, avg_price=self.best_bid(token_id) or 0.0)
        return fill

    def sell_proceeds(self, order_id: str, token_id: str) -> float | None:
        """卖出订单的真实到账（Polymarket 成交聚合）：Σ(price×size×(1−fee_bps/10000))。

        通过订单详情 associate_trades → 逐笔成交明细聚合；查询失败/无成交返回 None
        （调用方回退理论价差）。token_id 保留作参数签名，供未来直接按 token 查询兜底。
        """
        from py_clob_client_v2 import TradeParams

        client = self._get_client()
        try:
            detail = client.get_order(order_id)
            if not isinstance(detail, dict):
                return None
            total = 0.0
            for tid in detail.get("associate_trades") or []:
                rows = client.get_trades(TradeParams(id=tid), only_first_page=True)
                for t in rows or []:
                    price, size = t.get("price"), t.get("size")
                    if price is None or size is None:
                        continue
                    fee_bps = float(t.get("fee_rate_bps") or 0)
                    total += float(price) * float(size) * (1 - fee_bps / 10000)
            return total if total > 0 else None
        except Exception:
            return None

    def get_order(self, order_id: str) -> dict | None:
        """查询订单状态（成交检测）。"""
        return self._get_client().get_order(order_id)

    def fetch_token_trades(self, asset_id: str, after: int, before: int) -> list[dict]:
        """CLOB 时间窗成交查询（repair_trades 历史对账用，公开方法替代 _get_client 穿透）。"""
        from py_clob_client_v2 import TradeParams

        try:
            return self._get_client().get_trades(
                TradeParams(asset_id=asset_id, after=after, before=before),
                only_first_page=True,
            ) or []
        except Exception:
            return []

    def cancel(self, order_id: str) -> bool:
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


class SimExecutor:
    """dry-run 执行器：打印指令、按盘口模拟成交，不碰真实订单。

    盘口/凭证/采样器能力委托内部 live 实例（不暴露下单面）。
    """

    def __init__(
        self,
        private_key: str | None = None,
        creds_cache: str = "data/clob_creds.json",
        chain_id: int = 137,
        proxy_wallet: str | None = None,
        sampler: SamplerProto | None = None,
    ):
        self._live = ClobExecutor(
            private_key=private_key,
            creds_cache=creds_cache,
            chain_id=chain_id,
            proxy_wallet=proxy_wallet,
            sampler=sampler,
        )

    # ---- 委托：盘口 / 凭证 / 采样器 ----

    @property
    def sampler(self) -> SamplerProto | None:
        return self._live.sampler

    def attach_sampler(self, sampler: SamplerProto) -> None:
        self._live.attach_sampler(sampler)

    def fetch_book(self, token_id: str) -> dict:
        return self._live.fetch_book(token_id)

    def api_auth(self) -> dict | None:
        return self._live.api_auth()

    def best_ask(self, token_id: str) -> float | None:
        return self._live.best_ask(token_id)

    def best_bid(self, token_id: str) -> float | None:
        return self._live.best_bid(token_id)

    # ---- 模拟下单 ----

    def collateral_balance(self) -> float | None:
        """真实钱包余额（dry-run 仅展示用；主循环 dry-run 下 PnL 不用它）。"""
        return self._live.collateral_balance()

    def live_positions(self, user: str | None = None) -> list[dict]:
        """真实钱包持仓（dry-run 仅展示用，不参与模拟交易决策）。"""
        return self._live.live_positions(user)

    def settle_proceeds(self, condition_id: str) -> float | None:
        """真实兑付查询（dry-run 模拟仓与真实钱包无对应 conditionId → None 走理论价差）。"""
        return self._live.settle_proceeds(condition_id)

    def place_limit(self, token_id: str, side: str, price: float, size: float) -> str | None:
        """模拟挂限价单：规则校验与实盘一致（共用 validate_limit_order），返回模拟 id。"""
        validate_limit_order(size, price)
        print(f"[dry-run] 挂单 {side.upper()} {size} @ {price} token={token_id[:16]}...")
        return f"sim-{token_id[:8]}"

    def sell(self, token_id: str, size: float, price: float) -> str | None:
        return self.place_limit(token_id, "sell", price, size)

    def market_buy(self, token_id: str, amount: float) -> Fill | None:
        """模拟市价买入：按 best_ask 估算成交（结构与实盘一致：缺报价放弃建仓）。"""
        ask = self.best_ask(token_id)
        if ask is None:
            return None  # 无报价：不建仓（与实盘"缺成交数据放弃建仓"同语义）
        return Fill(order_id=f"sim-{token_id[:8]}", avg_price=ask, filled_size=amount / ask)

    def market_sell(self, token_id: str, size: float) -> Fill | None:
        print(f"[dry-run] 市价卖 {size:.4f} 股 token={token_id[:16]}...")
        return Fill(order_id=None, avg_price=self.best_bid(token_id))

    def sell_proceeds(self, order_id: str, token_id: str) -> float | None:
        return None  # 模拟无真实订单

    def get_order(self, order_id: str) -> dict | None:
        return None  # 模拟无真实订单

    def cancel(self, order_id: str) -> bool:
        print(f"[dry-run] 撤单 {order_id}")
        return True