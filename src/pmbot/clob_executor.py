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
    shares_for_amount,
    validate_limit_order,
)

logger = logging.getLogger(__name__)

# 决策价可接受的最大快照年龄（秒）。WS 断线时 BookSampler 后台 REST 兜底 1s
# 一轮，快照通常 ≤2s；放宽到 3s 使 "略陈旧" 不再触发同步 REST（那会在
# 盘口秒级极化时错过整个入场窗口）。超过此值才现拉，且用短超时快速失败。
# 注意：这是决策路径的新鲜度阈值，与 BookSampler 健康观测的 STALE_AGE_SEC
# （1s，connection_status/is_fresh）语义不同——健康观测更严格、决策有意容忍
# 以避免阻塞（见 executor_protocols.SamplerProto 注释）。
MAX_BOOK_AGE_SEC = 3.0
# 决策路径现拉盘口的超时（秒）：足够覆盖实测 0.7~1.3s 的正常往返，
# 又不至于让一次卡顿拖死 tick。
BOOK_REST_TIMEOUT_SEC = 2.0


class ClobExecutor:
    """实盘执行器：Polymarket CLOB 下单/撤单/卖出/盘口/余额。"""

    def __init__(
        self,
        private_key: str | None = None,
        creds_cache: str = "data/clob_creds.json",
        chain_id: int = 137,  # Polygon 主网；Amoy 测试网 80002
        proxy_wallet: str | None = None,
        sampler: SamplerProto | None = None,
    ) -> None:
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
        self._warmed_tokens: set[str] = set()  # 已预热元数据的 token（防每 tick 重复）

    def _sampler_snapshot(self, token_id: str) -> dict | None:
        return self._sampler.snapshot(token_id) if self._sampler else None

    def _sampler_update(self, token_id: str, book: dict) -> None:
        """REST 现拉结果回填采样器（防下个 tick 重复查询；采样器缺位时静默）。"""
        if self._sampler is None:
            return
        try:
            self._sampler.update_snapshot(token_id, book)
        except Exception:
            pass

    def _best_price(self, token_id: str, side: str, size: float) -> float | None:
        """可执行价（单一实现，best_ask/best_bid 共用）：内存快照 → 加权价。

        快路径：快照年龄 ≤ MAX_BOOK_AGE_SEC 直接用（WS 实时更新；断线时
        BookSampler 后台 REST 兜底 1s 一轮保持新鲜）。

        绝不在决策路径同步等 REST：盘口穿越后秒级极化，等几秒 = 放弃入场
        （回归：曾对陈旧快照直接 fetch_book，6s 超时下穿越瞬间的便宜档全部
        错过——WS 一断就永远开不了仓）。无快照/过旧时才短超时现拉（冷启动/
        新 token），失败即 None（宁缺毋滥，下 tick 重试）。
        """
        book = self._sampler_snapshot(token_id)
        age = self._sampler.snapshot_age(token_id) if self._sampler is not None else None
        if book is not None and (age is None or age <= MAX_BOOK_AGE_SEC):
            return weighted_price(book, side, size=size)
        try:
            book = self.fetch_book(token_id, timeout=BOOK_REST_TIMEOUT_SEC)
        except Exception:
            return None
        self._sampler_update(token_id, book)
        return weighted_price(book, side, size=size)

    @property
    def sampler(self) -> SamplerProto | None:
        """当前采样器引用（主循环订阅市场用；无采样器返回 None）。"""
        return self._sampler

    def attach_sampler(self, sampler: SamplerProto) -> None:
        """挂载 BookSampler（盘口快照优先读内存，REST 兜底）。"""
        self._sampler = sampler

    def fetch_book(self, token_id: str, timeout: float = 6.0) -> dict:
        """公开盘口查询（无需认证；BookSampler REST 兜底用）。

        走代理：clob.polymarket.com 直连在本机网络间歇不可达（实测直连/代理
        各有偶发超时，代理为既定路径且 WS 本就必须走它）。
        timeout 可调：后台兜底用默认 6s；决策路径传短超时（见 _best_price），
        禁止在盘口秒级极化时长时间阻塞。
        """
        import requests

        r = requests.get(
            f"{CLOB_HOST}/book?token_id={token_id}",
            timeout=timeout,
            proxies={"https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10808")},
            headers={"User-Agent": "pmbot/1.0"},
        )
        r.raise_for_status()
        return r.json()

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

    def _get_l1(self) -> ClobClient:
        if self._l1_client is None:
            if not self._pk:
                raise RuntimeError("缺少钱包私钥：请在 .env 中配置 PRIVATE_KEY")
            from py_clob_client_v2 import ClobClient

            self._l1_client = ClobClient(host=CLOB_HOST, chain_id=self._chain_id, key=self._pk)
        return self._l1_client

    def _get_client(self) -> ClobClient:
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

    def collateral_snapshot(self) -> tuple[float | None, dict]:
        """(抵押余额, 授权表) 一次查询（实盘自检用）；授权表形如 {合约: 额度}。

        授权为 0 表示未 approve、下单会被服务端拒——与余额同源一次取回，
        自检不必发两次请求。查询异常向上抛（调用方决定降级）。
        """
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        r = self._get_client().get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        if not isinstance(r, dict):
            return None, {}
        bal = r.get("balance")
        return (int(bal) / 1e6 if bal is not None else None), (r.get("allowances") or {})

    def collateral_balance(self) -> float | None:
        """代理钱包的 pUSD 抵押余额（最小单位 1e6）。"""
        return self.collateral_snapshot()[0]

    # ---- data-api 公开端点（/positions、/activity）----

    _DATA_API = "https://data-api.polymarket.com"

    def _data_api_get(self, path: str, params: dict) -> list[dict] | None:
        """data-api GET：代理/超时/错误归一集中一次；失败返回 None。

        代理默认 127.0.0.1:10808（本机环境，可用 HTTPS_PROXY 覆盖）——
        曾四处手抄同一 dict，换机器要改四处，收敛于此。
        """
        import requests

        try:
            r = requests.get(
                f"{self._DATA_API}{path}",
                params=params,
                proxies={"https": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:10808")},
                timeout=30,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            return data if isinstance(data, list) else None
        except Exception:
            return None

    def _activity_page(self, activity_type: str, offset: int = 0, limit: int = 500) -> list[dict]:
        """/activity 分页（type=TRADE/REDEEM）；失败返回 []（同步器静默跳过）。"""
        addr = self._proxy_wallet
        if not addr:
            return []
        data = self._data_api_get(
            "/activity",
            {"user": addr, "type": activity_type, "limit": limit, "offset": offset},
        )
        return data or []

    def live_positions(self, user: str | None = None) -> list[dict] | None:
        """Polymarket 实时持仓（官方 data-api /positions，user 过滤有效）。

        返回 [{asset, conditionId, size, avgPrice, curPrice, currentValue,
        cashPnl, realizedPnl, redeemable, title, outcome, ...}]；
        查询失败（网络/无地址）返回 None——调用方必须区分「无持仓」与「查询失败」。
        user 缺省用代理钱包地址（.env PROXY_WALLET）。
        """
        addr = user or self._proxy_wallet
        if not addr:
            return None
        return self._data_api_get("/positions", {"user": addr, "limit": 100})

    def settle_proceeds(self, condition_id: str) -> float | None:
        """结算兑付真实到账（data-api /activity REDEEM）：匹配 conditionId 的 REDEEM 记录 usdcSize。
        市场结算后赢的持仓自动兑付为 USDC（链上 REDEEM 交易），usdcSize 为实际到账
        金额（含本金）；结算记账用 到账 − 成本 替代理论价差，口径与钱包一致。
        查询失败/无匹配记录返回 None——调用方回退理论价差（与 sell_proceeds 同模式）。
        """
        addr = self._proxy_wallet
        if not addr or not condition_id:
            return None
        data = self._data_api_get(
            "/activity", {"user": addr, "type": "REDEEM", "limit": 200},
        )
        if not data:
            return None
        want = str(condition_id).lower()
        for a in data:
            if str(a.get("conditionId") or "").lower() == want:
                usdc = a.get("usdcSize")
                if usdc is not None:
                    return float(usdc)
        return None  # 有响应但该市场尚未 REDEEM（结算延迟/未兑付）

    @property
    def wallet_address(self) -> str | None:
        """代理钱包地址（交易历史同步用）。"""
        return self._proxy_wallet

    def fetch_trade_page(self, offset: int = 0, limit: int = 500) -> list[dict]:
        """成交流水分页（data-api /activity?type=TRADE，倒序最新在前）；失败返回 []。

        用 /activity 而非 /trades：前者带 usdcSize（含手续费的美元金额），
        统计/报表切 API 口径需要它。失败返回 []（同步器静默跳过）。
        """
        return self._activity_page("TRADE", offset, limit)

    def fetch_redeem_page(self, offset: int = 0, limit: int = 500) -> list[dict]:
        """结算兑付分页（data-api /activity?type=REDEEM，倒序最新在前）；失败返回 []。"""
        return self._activity_page("REDEEM", offset, limit)

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

    def warmup(self, token_ids: list[str]) -> None:
        """预热下单客户端的 token 元数据缓存（tick/fee/condition_id）。

        py_clob_client 的 __ensure_market_info_cached 按 token 缓存，而 momentum
        每窗口最多 1 笔、token 每窗口都新 → 每笔都是该 token 首单，缓存永远
        未命中，下单前要额外 2 次 REST（GET_MARKET_BY_TOKEN + GET_CLOB_MARKET）。
        窗口订阅时预热，把这两跳移出下单关键路径。失败静默（预热是优化，
        不是下单前置条件——缺失时下单路径会自行拉取）。

        调用方（subscribe_sampler）在每 tick 路径上，故用 _warmed_tokens 去重：
        每个 token 只在窗口首个 tick 实际发请求。
        """
        try:
            client = self._get_client()
        except Exception:
            return  # 凭证未就绪（未配置私钥/代理钱包）：跳过
        for tid in token_ids:
            if tid in self._warmed_tokens:
                continue
            try:
                client.get_tick_size(tid)  # 内部触发 __ensure_market_info_cached
                self._warmed_tokens.add(tid)
            except Exception as e:
                logger.debug("token 预热失败：%s: %s", tid[:16], e)

    def market_buy(self, token_id: str, amount: float, max_price: float | None = None) -> Fill | None:
        """市价买入（FOK）。amount 为美元金额（SDK 语义：BUY=$$$）。

        max_price: 显式保护价（最高可接受价）。不传时 SDK 自行拉盘口算
        “吃满 amount 的最贵档价”——对小单 ≈ best ask，等于不设限（决策时
        ≤cap、下单时已极化则照极化价成交）。传 entry_gate 上限，FOK 只在
        ≤上限时成交（宁错过不追高），并省掉 SDK 的 get_order_book 一次 REST。
        max_price=0.0 语义是「拒绝一切」（auto_tune 收窄结果），直接放弃下单：
        SDK 的 price=0 表示“不传、自动算吃穿价”，无法表达拒绝，会把保护价
        静默洗成不设限。

        返回 Fill（order_id/avg_price/filled_size）：优先取订单响应的
        averagePrice/matchedAmount，缺省时用 get_order 补查（以 API 为准，
        不靠本地盘口估算）。下单失败，或下单成功但 API 未返回实际成交数据
        （avg/size 缺）→ 返回 None：放弃建仓追踪，资金由 Polymarket 结算自动
        兑付（回归：盘口估算曾导致持仓股数记错 1.9231 vs 实际 1.7544）。
        """
        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side

        if max_price is not None and max_price <= 0:
            logger.warning("市价买入放弃：上限=%s 拒绝一切 token=%s",
                           max_price, token_id[:16])
            return None

        resp = self._get_client().create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=token_id,
                amount=amount,  # BUY: 美元金额（SDK 语义）
                side=Side.BUY,
                order_type=OrderType.FOK,
                price=max_price if max_price is not None else 0.0,  # 0 = 不传，SDK 自动算吃穿价
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
        def _f(x: object) -> float | None:
            try:
                return float(x) if x is not None else None
            except (TypeError, ValueError):
                return None

        return Fill(order_id=oid, avg_price=_f(avg), filled_size=_f(filled))

    def market_sell(self, token_id: str, size: float, min_price: float | None = None) -> Fill | None:
        """市价卖出持仓（FOK），返回 Fill（order_id/avg_price）。

        min_price: 显式保护价（最低可接受价）。不传时 SDK 拉盘口算“吃穿到
        size 的最低价”（对足量持仓 ≈ 不设限）；平仓只求成交，传 MIN_TICK_PRICE
        即“接受任何合法价”，与自动行为等价但省掉 get_order_book 一次 REST。

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
                price=min_price or 0.0,  # 0 = 不传，SDK 自动算吃穿价
            ),
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
        if not isinstance(resp, dict):
            return None
        fill = self._parse_fill(resp, side="sell")  # 卖单 making/taking 方向与买单相反
        if fill.avg_price is None:
            # 价格回退在成交语义内（调用方不再各自 best_bid）；按持仓量级定价
            fill = Fill(order_id=fill.order_id, avg_price=self.best_bid(token_id, size=size) or 0.0)
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

    def best_bid(self, token_id: str, size: float = 5.0) -> float | None:
        """持仓卖出可执行价（按 size 股可成交量加权，免疫垃圾挂单）。

        优先读 BookSampler 内存快照（高频采样），快照陈旧/缺失时 REST 现拉并回填；
        size 默认 5 股保守口径，平仓路径按持仓股数传入（决策价贴近实际可卖量）。
        """
        return self._best_price(token_id, "bids", size)

    def best_ask(self, token_id: str, size: float = 5.0) -> float | None:
        """买入可执行价（按 size 股可成交量加权，免疫垃圾挂单）。

        优先读 BookSampler 内存快照（高频采样），快照陈旧/缺失时 REST 现拉并回填；
        size 默认 5 股保守口径，开仓小单路径传 1.0 用最优档近似。
        """
        return self._best_price(token_id, "asks", size)


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
    ) -> None:
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

    def fetch_book(self, token_id: str, timeout: float = 6.0) -> dict:
        return self._live.fetch_book(token_id, timeout=timeout)

    def api_auth(self) -> dict | None:
        return self._live.api_auth()

    def best_ask(self, token_id: str, size: float = 5.0) -> float | None:
        return self._live.best_ask(token_id, size=size)

    def best_bid(self, token_id: str, size: float = 5.0) -> float | None:
        return self._live.best_bid(token_id, size=size)

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

    def warmup(self, token_ids: list[str]) -> None:
        """dry-run 无真实下单，不下单客户端预热（仅盘口定价用 _live，无需预热）。"""
        return

    def market_buy(self, token_id: str, amount: float, max_price: float | None = None) -> Fill | None:
        """模拟市价买入：按最优档估算成交（结构与实盘一致：缺报价放弃建仓）。

        小单（1 USDC ≈ 1-3 股）用 size=1.0（≈最优档价）估算，贴近实盘实际成交。
        max_price 为实盘 FOK 保护价（最高可接受价）：报价越界视为拒单，与实盘
        同语义（dry-run 不因未模拟保护价而高估成交率）。
        """
        ask = self.best_ask(token_id, size=1.0)
        if ask is None:
            return None  # 无报价：不建仓（与实盘"缺成交数据放弃建仓"同语义）
        if max_price is not None and ask > max_price:
            return None  # 保护价拒绝（与实盘 FOK 同语义：宁错过不追高）
        return Fill(order_id=f"sim-{token_id[:8]}", avg_price=ask,
                    filled_size=shares_for_amount(amount, ask))

    def market_sell(self, token_id: str, size: float, min_price: float | None = None) -> Fill | None:
        print(f"[dry-run] 市价卖 {size:.4f} 股 token={token_id[:16]}...")
        # 卖价取不到时回退 0.0（与实盘 market_sell 的 or 0.0 兜底一致，防成交语义分歧）
        return Fill(order_id=None, avg_price=self.best_bid(token_id, size=size) or 0.0)

    def sell_proceeds(self, order_id: str, token_id: str) -> float | None:
        return None  # 模拟无真实订单

    def get_order(self, order_id: str) -> dict | None:
        return None  # 模拟无真实订单

    def cancel(self, order_id: str) -> bool:
        print(f"[dry-run] 撤单 {order_id}")
        return True