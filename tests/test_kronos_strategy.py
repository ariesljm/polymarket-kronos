"""KronosStrategy 测试：信号生成（注入 fake 数据源与预测函数，不跑真实模型）。"""

from pathlib import Path

import pandas as pd
import pytest

from pmbot.config import StrategyConfig
from pmbot.strategy import create_strategy
from pmbot.strategies.kronos import KronosStrategy
from pmbot.types import Direction, Signal


def make_df(closes):
    n = len(closes)
    return pd.DataFrame(
        {
            "timestamp": [1_000_000 + i * 900_000 for i in range(n)],
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1.0] * n,
        }
    )


def make_strategy(tmp_path, closes, pred_closes, symbol="BTC", **kw):
    df = make_df(closes)

    class FakeDataSource:
        timeframe = "15m"

        def update(self, sym):
            return df

    sc = StrategyConfig(
        model_variant="kronos-mini",
        sample_count=20,
        max_klines=2048,
        market_interval="15m",
        thresholds=kw.pop("thresholds", None),
    )
    return KronosStrategy(
        strategy_config=sc,
        data_source=FakeDataSource(),
        predict_fn=lambda d, sample_count: pred_closes,
        log_dir=Path(tmp_path),
        symbol=symbol,
        settle_price_fn=kw.pop("settle_price_fn", lambda t: None),
        **kw,
    )


def test_p_up_and_direction_from_samples(tmp_path):
    strat = make_strategy(tmp_path, [100.0] * 10, [101.0, 102.0, 99.0])
    signal = strat.generate_signal()
    assert signal.direction is Direction.UP
    assert signal.p_up == pytest.approx(2 / 3)


def test_direction_down_when_p_up_below_half(tmp_path):
    strat = make_strategy(tmp_path, [100.0] * 10, [99.0, 98.0])
    signal = strat.generate_signal()
    assert signal.direction is Direction.DOWN
    assert signal.p_up == pytest.approx(0.0)


def test_tie_goes_down(tmp_path):
    # p_up == 0.5（平分）→ 归为 down（engine 层有阈值，不会真下注）
    strat = make_strategy(tmp_path, [100.0] * 10, [101.0, 99.0])
    signal = strat.generate_signal()
    assert signal.direction is Direction.DOWN
    assert signal.p_up == pytest.approx(0.5)


def test_in_progress_kline_dropped_from_baseline(tmp_path):
    # 末根 K 线进行中（openTime+15m > now）：基线用最后闭合 K 线，预测其下一根
    # 10 根闭合（末根 ts=9_100_000，current=100）+ 1 根进行中（ts=10_000_000，close=200 噪声）
    closes = [100.0] * 10 + [200.0]
    df = make_df(closes)

    class FakeDataSource:
        timeframe = "15m"

        def update(self, sym):
            return df

    strat = KronosStrategy(
        strategy_config=StrategyConfig(),
        data_source=FakeDataSource(),
        predict_fn=lambda d, sample_count: [101.0],
        log_dir=Path(tmp_path),
        symbol="BTC",
    )
    # now = 进行中 K 线开盘后 1 分钟（未闭合）
    signal = strat.generate_signal({"now_ms": 10_000_000 + 60_000})
    assert signal.direction is Direction.UP
    # 基线应为最后闭合 K 线 close=100（若未剔除进行中根，基线会是 200 → 101<200 → DOWN）
    assert signal.p_up == pytest.approx(1.0)


def test_empty_predictions_skip(tmp_path):
    strat = make_strategy(tmp_path, [100.0] * 10, [])
    signal = strat.generate_signal()
    assert signal.direction is Direction.SKIP


def test_registered_as_kronos(tmp_path):
    strat = create_strategy("kronos")
    assert isinstance(strat, KronosStrategy)


def test_all_signals_recorded(tmp_path):
    """每次预测都记录（含中间带信号），用于统计模型整体方向准确率。"""
    strat = make_strategy(tmp_path, [100.0] * 10, [101.0] * 7 + [99.0] * 8,
                          thresholds={"p_up_buy": 0.6, "p_down_buy": 0.4})
    signal = strat.generate_signal()
    assert signal.direction is Direction.DOWN  # p_up = 7/15 ≈ 0.467 < 0.5
    # 所有预测都应被记录（无论是否达交易阈值）
    assert len(strat.log._load()) == 1


def test_tradeable_signal_recorded(tmp_path):
    """达阈值信号正常记录（进入准确率评估）。"""
    strat = make_strategy(tmp_path, [100.0] * 10, [101.0] * 10,
                          thresholds={"p_up_buy": 0.6, "p_down_buy": 0.4})
    strat.generate_signal()
    assert len(strat.log._load()) == 1


def test_no_thresholds_records_all(tmp_path):
    """thresholds=None（回测/兼容）：全部信号记录。"""
    strat = make_strategy(tmp_path, [100.0] * 10, [101.0] * 7 + [99.0] * 8)
    strat.generate_signal()
    assert len(strat.log._load()) == 1


def _make_rolling_ds(dfs):
    """FakeDataSource：每次 update 返回下一段数据（模拟窗口推进）。"""
    class FakeDataSource:
        timeframe = "15m"
        def __init__(self, dfs):
            self._dfs = dfs
            self._i = 0
        def update(self, sym):
            df = self._dfs[min(self._i, len(self._dfs) - 1)]
            self._i += 1
            return df
    return FakeDataSource(dfs)


def test_settle_price_fn_feeds_evaluation(tmp_path):
    """注入的 settle_price_fn 均价进入评估：高于基线则模型方向判对（TWAP 口径）。"""
    dfs = [
        make_df([100.0] * 10),               # 首轮：预测 ts=10_000_000（baseline 100）
        make_df([100.0] * 10 + [102.0]),     # 推进：次轮记录新目标
        make_df([100.0] * 10 + [102.0, 102.0]),  # 再推进：首轮目标闭合成评估
    ]
    strat = KronosStrategy(
        strategy_config=StrategyConfig(model_variant="kronos-mini", sample_count=20,
                                       max_klines=2048, market_interval="15m"),
        data_source=_make_rolling_ds(dfs),
        predict_fn=lambda d, sample_count: [101.0],
        log_dir=Path(tmp_path),
        symbol="BTC",
        settle_price_fn=lambda t: 101.0,  # 结算均价 101 > baseline 100 → up
    )
    strat.generate_signal()
    strat.generate_signal()
    strat.generate_signal()
    acc = strat.log.accuracy()
    assert acc["total"] == 1
    assert acc["correct"] == 1  # 预测 up，TWAP 口径 101>100 up → 对


def test_settle_price_fn_low_flips_to_wrong(tmp_path):
    """结算均价低于基线 → TWAP 口径 down，预测 up 判错（覆盖 close 口径误判）。"""
    dfs = [
        make_df([100.0] * 10),
        make_df([100.0] * 10 + [102.0]),
        make_df([100.0] * 10 + [102.0, 102.0]),
    ]
    strat = KronosStrategy(
        strategy_config=StrategyConfig(model_variant="kronos-mini", sample_count=20,
                                       max_klines=2048, market_interval="15m"),
        data_source=_make_rolling_ds(dfs),
        predict_fn=lambda d, sample_count: [101.0],
        log_dir=Path(tmp_path),
        symbol="BTC",
        settle_price_fn=lambda t: 99.0,  # 结算均价 99 < baseline 100 → down
    )
    strat.generate_signal()
    strat.generate_signal()
    strat.generate_signal()
    acc = strat.log.accuracy()
    assert acc["total"] == 1
    assert acc["correct"] == 0  # 预测 up，TWAP 口径 down → 错
