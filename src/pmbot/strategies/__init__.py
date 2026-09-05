"""策略注册入口：导入各策略触发 @register。

新增策略：在策略文件用 @register("名字") 注册，然后在本模块 import 一行。
config.yaml 顶层 strategy: 名字 即可切换（参数放同名分节）。
"""

from pmbot.strategies import momentum  # noqa: F401