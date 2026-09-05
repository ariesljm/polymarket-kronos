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

    def refresh_signal(self, context: SignalContext | None = None) -> Signal | None:
        """窗口内的信号刷新钩子（每 tick 调用一次）。

        默认不刷新（None）——窗口信号只在开始时生成一次（静态预测模型，
        预测在窗口开盘时做，之后维持原判断）。返回非 None Signal 则更新当前信号
        （本窗口未下注且无持仓时生效）。momentum 等事件驱动策略用它实现在
        窗口内持续检测穿越/突破，随时改变入场意图。
        """
        return None

    def reset_runtime_data(self) -> None:
        """清空策略运行时数据（K线/预测记录）；无持久化状态的策略无需覆写。"""

    def window_outcome(self, window_start: int) -> Direction | None:
        """窗口实际结算方向（UP/DOWN）；不支持的策略返回 None。

        模拟环境（dry-run）结算判定单一事实源：窗口实际涨跌由标的 K 线决定，
        不依赖真实 Polymarket 市场结算。返回 None 时 Settler 回退 gamma 流程。
        """
        return None

    def status_text(self) -> str | None:
        """策略专属状态文案（面板/TUI 展示）；无专属状态的策略返回 None。

        momentum 用它显示窗口基准价 / 实时偏离 / 穿越阈值等运行时状态；
        无持久运行时状态的策略无需覆写。
        """
        return None



def register(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
    """注册策略类到工厂（装饰器用法）。"""

    def deco(cls: type[Strategy]) -> type[Strategy]:
        _REGISTRY[name] = cls
        return cls

    return deco


def strategies() -> tuple[str, ...]:
    """已注册策略名（配置校验/候选列表单一事实源）。"""
    return tuple(sorted(_REGISTRY))


def strategy_class(name: str) -> type[Strategy]:
    """已注册策略类（构造前置依赖注入判断用；未注册名抛 ValueError）。"""
    if name not in _REGISTRY:
        _load_strategy_modules()
    if name not in _REGISTRY:
        raise ValueError(f"未知 strategy: {name}，已注册: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


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
