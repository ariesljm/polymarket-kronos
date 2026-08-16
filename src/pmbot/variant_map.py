"""Kronos 模型变体 → 上下文长度映射（事实源）。

依据上游 shiyu-coder/Kronos README Model Zoo 与 vendored 推理代码：
模型只消费最近 max_context 根 K 线（buffer_len = min(initial_seq_len, max_context)），
故数据拉取/存储数量应跟随变体：kronos-mini=2048，kronos-small/base=512。
config 与 predictor 均从此处读取，避免 config 层依赖 torch。
"""

VARIANT_CONTEXT = {
    "kronos-mini": 2048,
    "kronos-small": 512,
    "kronos-base": 512,
}

# 变体 → (本地模型目录, 本地 tokenizer 目录, HF 权重 repo 前缀)
# 上游: shiyu-coder/Kronos（金融 K 线专用模型，45+ 全球交易所训练），
# HF 权重组织为 NeoQuasar（amazon-science 下不存在同名模型，勿改回）
VARIANT_WEIGHTS = {
    "kronos-mini": ("kronos-mini", "kronos-tokenizer-2k", "NeoQuasar"),
    "kronos-small": ("kronos-small", "kronos-tokenizer-base", "NeoQuasar"),
    "kronos-base": ("kronos-base", "kronos-tokenizer-base", "NeoQuasar"),
}

DEFAULT_VARIANT = "kronos-mini"
