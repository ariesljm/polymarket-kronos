"""vendor 目录说明

本目录为 Amazon Science Kronos 开源模型的上游代码（复制自 ariesljm/Kronos 的 vendor 目录，同源于 shiyu-coder/Kronos）（原 Kronos 仓库
model/kronos.py、model/module.py），为满足"无 GPU 依赖、边缘节点可运行、
离线部署"目标而 vendored 进项目。

豁免依据：本目录代码逐行复制自上游仓库，未作修改，因此保留上游英文
docstring / assert 消息，不适用 AGENTS.md 的"注释一律简体中文"规则；
项目自身代码（src/ 其余模块）仍须遵守该规则。
"""

from .kronos import KronosTokenizer, Kronos, KronosPredictor
