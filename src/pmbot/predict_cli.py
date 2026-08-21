"""CLI：跑一次真实预测（区别于 predictor.py 的推理客户端，本模块是命令入口）。

用法: uv run python -m pmbot.predict_cli [config.yaml 路径]
输出策略信号（方向 + P(up)）与当前方向准确率。
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑一次 Kronos 预测")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args(argv)

    from pmbot.config import load_config
    from pmbot.strategy import create_strategy

    cfg = load_config(args.config)
    strat = create_strategy(
        cfg.strategy,
        symbol=cfg.symbols[0],
        config=cfg.to_strategy_config(),
    )
    signal = strat.generate_signal()
    acc = strat.log.accuracy()

    print(f"方向: {signal.direction.value}  P(up): {signal.p_up:.3f}")
    print(f"方向准确率: {acc['correct']}/{acc['total']} = {acc['accuracy']:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
