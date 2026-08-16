# 02 — Kronos 策略（Binance 数据源 + CSV 滚动存储 + Kronos 推理）

**What to build:** 实现 `Strategy` 接口的第一个实例：从 Binance 增量拉取 15m K 线（首次全量回填、之后每窗口只拉增量，落 CSV 滚动存储——每标的一个文件，超过 `max_klines`（默认 2048）裁剪头部）；本地 CPU 运行 Kronos 模型（shiyu-coder model 包，`KronosPredictor` 多路径采样，模型变体可配置，默认 kronos-mini）计算 `P(up)` 并输出 `Signal`；单独记录"预测方向 vs 实际方向"的模型准确率（与交易结果解耦）。

**Blocked by:** 01 — 项目骨架 + 配置 + Strategy 接口 + 决策引擎

**Status:** resolved

- [ ] CLI 可跑一次真实预测并输出 Signal（方向 + P(up)）
- [ ] 连续跑两次：第二次仅增量拉取（日志可证）
- [ ] CSV 滚动存储：超上限正确裁剪；列符合约定（timestamp, open, high, low, close, volume）
- [ ] 模型变体/采样路径数等从配置读取
- [ ] 方向准确率记录正确（对照真实后续 K 线）

## Comments

- 2026-08-14 实现完成：66 测试全绿（774d7df）
- 数据源：requests 直连 Binance 公开接口 data-api.binance.vision（用户要求不用 ccxt，
  主站 api.binance.com 不可达）；首次全量回填 + 增量拉取（日志可证）
- 数据量跟随模型变体：max_klines 默认 = 变体上下文（mini 2048 / small+base 512），
  依据上游 README Model Zoo 与 vendored 代码 buffer_len=min(seq, max_context) 确认
- P(up)：KronosPredictor 的 sample_count 是多路径求平均（已核实 vendored 代码），
  故改为 N 次独立采样（sample_count=1）取预测涨的占比
- code-review 修复：进行中 K 线提前结算（target>=latest 跳过）、数据缺口 KeyError、
  窗口语义对齐（剔除进行中 K 线）、predictor 客户端缓存、STEP_MS 收敛到 constants
