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


def test_settle_price_used_when_provided(log):
    # 结算均价 99 < baseline 100（TWAP 口径 down），但 close 105 > 100（close 口径 up）
    # → 用 TWAP 口径判定，预测 up 错
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, _ = log.evaluate(make_df([100.0, 105.0, 105.0]), settle_prices={1_900_000: 99.0})
    assert (correct, total) == (0, 1)


def test_settle_price_flips_wrong_to_correct(log):
    # close 95 < baseline 100（close 口径 down），但结算均价 101 > 100（TWAP 口径 up）
    # → 用 TWAP 口径判定，预测 up 对
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, _ = log.evaluate(make_df([100.0, 95.0, 95.0]), settle_prices={1_900_000: 101.0})
    assert (correct, total) == (1, 1)


def test_settle_price_missing_window_deferred(log):
    # 启用 TWAP 口径（settle_prices 非空）但该目标窗口均价缺失（未最终化/拉取失败）
    # → 暂不评估（evaluated 保持 0），留待下轮重试；不用 close 近似污染统计
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, _ = log.evaluate(make_df([100.0, 105.0, 105.0]), settle_prices={9_999_999: 99.0})
    assert (correct, total) == (0, 0)
    assert log.pending_targets() == [1_900_000]


def test_settle_prices_empty_dict_falls_back_close(log):
    # settle_prices 空 dict（未启用 TWAP 口径）→ close 口径判定（105 > 100 up）
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    correct, total, _ = log.evaluate(make_df([100.0, 105.0, 105.0]), settle_prices={})
    assert (correct, total) == (1, 1)


def test_pending_targets_lists_unsettled(log):
    log.record(1_900_000, Direction.UP, 0.7, 100.0)
    log.record(2_800_000, Direction.DOWN, 0.2, 105.0)
    assert sorted(log.pending_targets()) == [1_900_000, 2_800_000]
    log.evaluate(make_df([100.0, 105.0, 100.0, 100.0]))
    assert log.pending_targets() == []
