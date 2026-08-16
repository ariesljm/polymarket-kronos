"""回测脚本纯函数测试（真实模型推理不 mock，靠人工运行脚本验证）。"""

import pandas as pd

from pmbot.backtest import evaluate_predictions, split_points


def test_split_points_takes_last_n():
    df = pd.DataFrame({"timestamp": range(600)})
    pts = split_points(df, 50, 512)
    assert pts == list(range(550, 600))
    assert len(pts) == 50


def test_split_points_requires_context():
    df = pd.DataFrame({"timestamp": range(100)})
    pts = split_points(df, 50, 512)
    assert len(pts) == 50  # 少于上下文时仍取最后 50（数据不足由调用方过滤）


def test_evaluate_predictions_correct_up():
    preds = [100.0, 101.0, 102.0]  # 全部 > baseline 100
    ok, actual_up = evaluate_predictions(preds, baseline=100.0, actual_close=103.0)
    assert ok is True
    assert actual_up is True


def test_evaluate_predictions_wrong_down():
    preds = [99.0, 98.0]  # 全部 < baseline 100 → 预测跌
    ok, actual_up = evaluate_predictions(preds, baseline=100.0, actual_close=103.0)
    assert ok is False  # 实际涨，预测跌


def test_evaluate_predictions_majority_vote():
    preds = [101.0, 99.0, 102.0, 98.0, 103.0]  # 3/5 涨
    ok, _ = evaluate_predictions(preds, baseline=100.0, actual_close=105.0)
    assert ok is True
