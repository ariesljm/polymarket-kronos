"""variant_map 测试：变体上下文长度映射（数据拉取数量的事实源）。"""

import pytest

from pmbot.variant_map import VARIANT_CONTEXT, DEFAULT_VARIANT


def test_context_lengths_match_model_zoo():
    # 依据上游 shiyu-coder/Kronos README Model Zoo：mini=2048, small/base=512
    assert VARIANT_CONTEXT["kronos-mini"] == 2048
    assert VARIANT_CONTEXT["kronos-small"] == 512
    assert VARIANT_CONTEXT["kronos-base"] == 512


def test_default_variant_is_mini():
    assert DEFAULT_VARIANT == "kronos-mini"


def test_weights_repo_is_neoquasar():
    """HF 权重组织为 NeoQuasar（amazon-science 下不存在，曾导致下载失败）。"""
    from pmbot.variant_map import VARIANT_WEIGHTS

    for variant, (_, _, repo) in VARIANT_WEIGHTS.items():
        assert repo == "NeoQuasar", f"{variant} 的 repo 应为 NeoQuasar"
