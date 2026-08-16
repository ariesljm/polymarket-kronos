"""策略接口与注册工厂。

Strategy 是信号源接口：消费数据上下文，输出方向信号。
执行层（挂单/止盈/止损/熔断）由决策引擎共享，与具体策略无关。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from pmbot.types import Signal, SignalContext

_REGISTRY: dict[str, type["Strategy"]] = {}


class Strategy(ABC):
    """策略接口：生成交易信号。"""

    @abstractmethod
    def generate_signal(self, context: SignalContext | None = None) -> Signal: ...


def register(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    """注册策略类到工厂（装饰器用法）。"""

    def deco(cls: type[Strategy]) -> type[Strategy]:
        _REGISTRY[name] = cls
        return cls

    return deco


def create_strategy(name: str, **kwargs) -> Strategy:
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
