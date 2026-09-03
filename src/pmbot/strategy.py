"""策略接口与注册工厂。

Strategy 是信号源接口：消费数据上下文，输出方向信号。
执行层（挂单/止盈/止损/熔断）由决策引擎共享，与具体策略无关。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from pmbot.types import Direction, Signal, SignalContext

_REGISTRY: dict[str, type["Strategy"]] = {}


class Strategy(ABC):
    """策略接口：生成交易信号。"""

    @abstractmethod
    def generate_signal(self, context: SignalContext | None = None) -> Signal: ...

    def reset_runtime_data(self) -> None:
        """清空策略运行时数据（K线/预测记录）；无持久化状态的策略无需覆写。"""

    def window_outcome(self, window_start: int) -> Direction | None:
        """窗口实际结算方向（UP/DOWN）；不支持的策略返回 None。

        模拟环境（dry-run）结算判定单一事实源：窗口实际涨跌由标的 K 线决定，
        不依赖真实 Polymarket 市场结算。返回 None 时 Settler 回退 gamma 流程。
        """
        return None



def register(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    """注册策略类到工厂（装饰器用法）。"""

    def deco(cls: type[Strategy]) -> type[Strategy]:
        _REGISTRY[name] = cls
        return cls

    return deco


def create_strategy(name: str, *, config=None, **kwargs) -> Strategy:
    """按注册名创建策略；config 为 StrategyConfig 窄视图时打包传给策略构造。"""
    if config is not None:
        kwargs.setdefault("strategy_config", config)
    if name not in _REGISTRY:
        _load_strategy_modules()
    if name not in _REGISTRY:
        raise ValueError(f"未知 strategy: {name}，已注册: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def _load_strategy_modules() -> None:
    try:
        import pmbot.strategies  # noqa: F401  触发各策略的 @register
    except ImportError:
        pass
