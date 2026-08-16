"""KronosStrategy 测试：信号生成（注入 fake 数据源与预测函数，不跑真实模型）。"""

from pathlib import Path

import pandas as pd
import pytest

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


def make_strategy(tmp_path, closes, pred_closes, symbol="BTC"):
    df = make_df(closes)

    class FakeDataSource:
        timeframe = "15m"

        def update(self, sym):
            return df

    return KronosStrategy(
        data_source=FakeDataSource(),
        predict_fn=lambda d, sample_count: pred_closes,
        log_dir=Path(tmp_path),
        symbol=symbol,
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
