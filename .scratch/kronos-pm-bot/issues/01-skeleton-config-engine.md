# 01 — 项目骨架 + 配置 + Strategy 接口 + 决策引擎

**What to build:** 搭建 `uv` 管理的 Python 项目骨架；实现 `config.yaml` 加载与校验（策略参数按策略名分节，全部可配置）；定义 `Strategy` 接口（`generate_signal → Signal{direction, p_up}`）与策略工厂；实现决策引擎纯函数 `decide(state, market, signal) → Action`（挂限价/撤单/止盈卖出/止损卖出/跳过/暂停）与 Action 枚举；数据源、执行、时钟全部做成可注入的 fake 适配器，使策略逻辑可在无网络、无真钱条件下完整测试。

**Blocked by:** None — can start immediately

**Status:** resolved

- [ ] `uv` 项目骨架可运行（测试可执行）
- [ ] 配置加载与校验：非法参数启动报错；策略参数可按策略名分节读取
- [ ] Strategy 接口 + 工厂：按配置实例化策略，默认 kronos
- [ ] 决策引擎单元测试覆盖（全部经注入的纯数据输入，无网络/真钱）：
  - 信号阈值边界：P(up)>60% 买 Up（恰好 60% 不买）、P(up)<40% 买 Down（恰好 40% 不买）、中间地带跳过
  - 挂单：按配置价（0.45）挂限价
  - 撤单：窗口结束前 3 分钟未成交 → 撤单放弃
  - 止盈：价格 ≥0.80 → SELL take_profit
  - 止损：价格 ≤0.30 → SELL stop_loss；窗口过 10 分钟未盈利 → SELL time_stop
  - 熔断：连亏 10 笔或单日亏 10 USDC → PAUSE；暂停后可手动恢复
  - 每窗口每标的最多一注（不重复下单）

## Comments

- 2026-08-14 实现完成：36 测试全绿（cba4a02 + 5956b78）
- code-review 双轴审查发现并修复：时间止损测试假阳性、config 非数字参数异常类型、
  p_down_sell 命名歧义、人工暂停接线、Signal 类型规范化
- 阈值语义定稿：严格 >60% 买 Up / <40% 买 Down（恰好值跳过）
