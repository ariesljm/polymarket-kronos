"""CLI：生成验证报告。

用法: uv run python -m pmbot.report [--config config.yaml]
从 data/trades.csv 与方向准确率记录生成三指标验证报告。
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成验证报告")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--out", default="data/report.md", help="报告输出路径")
    args = parser.parse_args(argv)

    from pmbot.config import load_config
    from pmbot.prediction_log import PredictionLog
    from pmbot.stats import compute_stats, write_report

    cfg = load_config(args.config)
    symbol = cfg.symbols[0]
    acc = PredictionLog("data", symbol).accuracy()
    stats = compute_stats("data/trades.csv", acc)
    params = {
        "amount_per_trade": cfg.amount_per_trade,
        "p_up_buy": cfg.p_up_buy,
        "p_down_buy": cfg.p_down_buy,
        "take_profit": cfg.take_profit,
        "stop_loss": cfg.stop_loss,
        "model_variant": cfg.model_variant,
    }
    report = write_report(
        stats, symbol=symbol, strategy=cfg.strategy, params=params, path=args.out
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
