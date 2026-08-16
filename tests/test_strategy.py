"""策略接口与工厂测试。

测试接缝：create_strategy —— 按名称实例化已注册策略，未注册名称明确报错。
真实策略（kronos）在票 02 实现；此处用测试内注册的 FakeStrategy 验证机制。
"""

import pytest

from pmbot.strategy import Strategy, create_strategy, register
from pmbot.types import Signal


class FakeStrategy(Strategy):
    def generate_signal(self, context) -> Signal:
        return Signal(direction="skip", p_up=0.5)


def test_register_and_create():
    register("fake")(FakeStrategy)
    strat = create_strategy("fake")
    assert isinstance(strat, FakeStrategy)
    assert isinstance(strat.generate_signal({}), Signal)


def test_unknown_strategy_raises():
    with pytest.raises(ValueError, match="fake-unknown"):
        create_strategy("fake-unknown")
