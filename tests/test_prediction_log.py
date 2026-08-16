"""PredictionLog 测试：预测方向 vs 实际方向的方向准确率。"""

from pathlib import Path

import pandas as pd
import pytest

from pmbot.prediction_log import PredictionLog
from pmbot.types import Direction


@pytest.fixture
def log(tmp_path):
    return PredictionLog(Path(tmp_path))


def make_df(closes, start_ts=1_000_000):
    n = len(closes)
    return pd.DataFrame(
        {
            "timestamp": [start_ts + i * 900_000 for i in range(n)],
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1.0] * n,
        }
    )


def test_correct_prediction_counts(log):
    # 记录 ts=1_900_000（预测目标 K 线时间戳，即预测窗口开始），close=105 > baseline 100 → 涨 ✓
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, acc = log.evaluate(make_df([100.0, 105.0, 105.0]))
    assert (correct, total) == (1, 1)
    assert acc == pytest.approx(1.0)


def test_wrong_prediction_counts(log):
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, acc = log.evaluate(make_df([100.0, 95.0, 95.0]))
    assert (correct, total) == (0, 1)


def test_unsettled_prediction_not_counted(log):
    # 目标 K 线（ts=1_900_000）未闭合（target == latest，最后一根是进行中 K 线）→ 不参与统计
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, acc = log.evaluate(make_df([100.0, 105.0]))
    assert (correct, total) == (0, 0)


def test_target_older_than_latest_is_evaluated(log):
    # 目标 K 线已闭合（target < latest，存在更晚的 K 线）→ 正常评估
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, acc = log.evaluate(make_df([100.0, 105.0, 107.0]))
    assert (correct, total) == (1, 1)
    assert acc == pytest.approx(1.0)


def test_gap_in_klines_skipped_not_crash(log):
    # 目标 K 线（ts=1_900_000）被滚动裁剪掉（数据缺口）→ 跳过，不崩溃
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, acc = log.evaluate(make_df([100.0, 105.0, 106.0], start_ts=2_800_000))
    assert (correct, total) == (0, 0)


def test_accuracy_accumulates_across_records(log):
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    log.record(2_800_000, Direction.DOWN, 0.2, 105.0)
    # 实际: ts=1_900_000 close=105 → 涨，第一条（预测涨）对；ts=2_800_000 close=100 → 跌，第二条（预测跌）对
    correct, total, acc = log.evaluate(make_df([100.0, 105.0, 100.0, 100.0]))
    assert (correct, total) == (2, 2)
    assert acc == pytest.approx(1.0)


def test_accuracy_is_persistent_across_instances(log, tmp_path):
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    log2 = PredictionLog(Path(tmp_path))
    correct, total, _ = log2.evaluate(make_df([100.0, 105.0, 105.0]))
    assert (correct, total) == (1, 1)


def test_direction_accuracy_reported(log):
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    log.evaluate(make_df([100.0, 105.0, 105.0]))
    stats = log.accuracy()
    assert stats["correct"] == 1
    assert stats["total"] == 1
    assert stats["accuracy"] == pytest.approx(1.0)
