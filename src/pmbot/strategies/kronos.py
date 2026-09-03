"""Kronos 策略：Binance 数据 → Kronos 模型推理 → 方向信号。

首个 Strategy 实现（注册名 kronos）。数据源与预测函数可注入（测试用 fake）；
真实预测走 KronosPredictorClient（GPU 自动检测/CPU 回退，模型权重在 models/）。

窗口语义：generate_signal 在窗口开盘时调用，传入当前时间（context["now_ms"]）。
若 Binance 最新一根 K 线是进行中的（openTime+15m > now），将其剔除，
用最后闭合 K 线做基线、预测下一个 15m 窗口方向，与 Polymarket 窗口对齐。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from pmbot.config import StrategyConfig
from pmbot.constants import step_ms_for
from pmbot.data_source import BinanceDataSource, KlineStore, fetch_klines_batch
from pmbot.prediction_log import PredictionLog
from pmbot.strategy import Strategy, register
from pmbot.types import Direction, Signal, SignalContext


@register("kronos")
class KronosStrategy(Strategy):
    def __init__(
        self,
        *,
        strategy_config: StrategyConfig | None = None,
        data_source: BinanceDataSource | None = None,
        predict_fn: Callable | None = None,
        settle_price_fn: Callable | None = None,
        log_dir: str | Path = "data",
        symbol: str = "BTC",
    ):
        """strategy_config: 策略参数窄视图（模型变体/采样数/上下文长度/间隔/阈值），
        来自 Config.to_strategy_config()，避免 10 参数构造签名。

        依赖注入（data_source / predict_fn / settle_price_fn）与配置（strategy_config）分离：
        测试注入 fake，运行态由工厂（create_strategy）构造真实依赖。

        settle_price_fn: 目标窗口 → 窗口最后 60s 均价（Polymarket 60s Chainlink TWAP 结算
        口径近似）；None=内置网络实现（拉 Binance 1m K 线），显式传返回 None 的 callable
        可禁用（评估回退 close 口径）。
        """
        sc = strategy_config or StrategyConfig()
        self.data_source = data_source or BinanceDataSource(
            KlineStore(log_dir, timeframe=sc.market_interval), max_klines=sc.max_klines,
            timeframe=sc.market_interval,
        )
        # 真实预测客户端在构造时创建（惰性加载权重），避免每次信号生成重载模型
        self._predict_fn = predict_fn
        self._real_predictor = None
        self.log = PredictionLog(log_dir, symbol)
        self.symbol = symbol
        self.sample_count = sc.sample_count
        self.variant = sc.model_variant
        self.thresholds = sc.thresholds
        # 结算参考价获取器（None=内置网络实现）
        self._settle_price_fn = settle_price_fn

    def generate_signal(self, context: SignalContext | None = None) -> Signal:
        now_ms = (context or {}).get("now_ms") or int(time.time() * 1000)
        df = self.data_source.update(self.symbol)
        self.log.evaluate(df, self._settle_prices())

        # 剔除进行中的最后一根 K 线，保证基线/预测目标与窗口对齐
        last_ts = int(df["timestamp"].iloc[-1])
        if len(df) > 1 and last_ts + step_ms_for(self.data_source.timeframe) > now_ms:
            df = df.iloc[:-1]

        predict = self._predict_fn or self._get_real_predictor()
        preds = predict(df, self.sample_count)
        if not preds:
            return Signal(direction=Direction.SKIP, p_up=0.5)
        current = float(df["close"].iloc[-1])
        p_up = sum(1 for p in preds if p > current) / len(preds)
        direction = Direction.UP if p_up > 0.5 else Direction.DOWN
        # 记录每次预测结果（含中间带信号），用于统计模型整体方向准确率
        target_ts = int(df["timestamp"].iloc[-1]) + step_ms_for(self.data_source.timeframe)
        self.log.record(target_ts, direction, p_up, current)
        return Signal(direction=direction, p_up=p_up, baseline_close=current)

    def window_outcome(self, window_start: int) -> Direction | None:
        """窗口实际结算方向：用 5m K 线收盘价 vs 窗口起点价判定涨跌。

        与 PredictionLog.evaluate 同源（baseline = 窗口前一根 K close，
        结束价 = 窗口那根 K close）。增量拉取保证窗口 K 已闭合后可得；
        K 线缺失/拉取失败返回 None，Settler 回退 gamma 流程。
        """
        step_ms = step_ms_for(self.data_source.timeframe)
        try:
            df = self.data_source.update(self.symbol)
        except Exception:
            return None  # 网络拉取失败：暂不判定，下 tick 重试（回退 gamma）
        ts_close = dict(zip(df["timestamp"].astype(int), df["close"].astype(float)))
        window_ms = int(window_start) * 1000
        baseline = ts_close.get(window_ms - step_ms)
        end_close = ts_close.get(window_ms)
        if baseline is None or end_close is None:
            return None
        return Direction.UP if end_close > baseline else Direction.DOWN

    def _settle_prices(self) -> dict[int, float]:
        """为未评估预测构造 {目标窗口 ts: 窗口最后 60s 均价}（TWAP 结算口径）。

        拉取失败/数据未就绪的窗口不入 dict（evaluate 对该窗口暂缓评估，下轮重试；
        宁缺毋滥，不用错误时效数据污染准确率）。
        """
        fetch = self._settle_price_fn or self._default_settle_price
        prices: dict[int, float] = {}
        for target_ts in self.log.pending_targets():
            try:
                v = fetch(target_ts)
            except Exception:
                v = None
            if v is not None:
                prices[target_ts] = float(v)
        return prices

    def _default_settle_price(self, target_ts: int) -> float | None:
        """内置结算参考价：目标窗口最后 60 秒均价（近似 Chainlink 60s TWAP）。

        拉取下界覆盖窗口末段的 Binance 1m K 线，取最后 60 秒那根的 HLOC 平均；
        拉取失败/数据未就绪/末段 K 未最终化（close 与当前时间太近）返回 None
        （evaluate 对该窗口暂缓评估）。
        """
        import requests
        import time

        end = target_ts + step_ms_for(self.data_source.timeframe)
        try:
            ks = fetch_klines_batch(self.symbol, "1m", end - 60_000, 10, proxies=None)
        except (requests.RequestException, ValueError, IndexError):
            return None
        # 窗口 [target_ts, end) 的最后 60 秒 = 覆盖 [end-60s, end) 的单根 1m K
        last = [k for k in ks if end - 60_000 <= k.timestamp < end]
        if not last:
            return None
        k = last[-1]
        # 末段 K 若仍在爆涨帧内（窗口刚结束未最终化）→ 暂缓，避免用中间值污染统计
        if end > int(time.time() * 1000) - 30_000:
            return None
        return (k.open + k.high + k.low + k.close) / 4.0

    def reset_runtime_data(self) -> None:
        """清除本策略的 K 线与预测记录（下次信号生成自动回填）。"""
        self.log.reset()
        self.data_source.store.reset(self.symbol)

    def _get_real_predictor(self):
        if self._real_predictor is None:
            from pmbot.predictor import KronosPredictorClient

            self._real_predictor = KronosPredictorClient(variant=self.variant).predict_closes
        return self._real_predictor
