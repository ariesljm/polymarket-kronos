# Spec: Kronos 驱动的 Polymarket BTC 15m Up/Down 交易系统

Status: ready-for-agent
Feature: kronos-pm-bot
Date: 2026-08-14

## Problem Statement

用户想验证开源时序基础模型 **Kronos**（shiyu-coder/Kronos，AAAI 2026）对加密货币 15 分钟涨跌方向的预测能力，在 Polymarket 的 **BTC 15-min Up/Down** 市场上用真钱小规模全自动交易，判断它是否有 edge。Kronos 模型本身不连任何交易所，下单执行（Polymarket CLOB）需要全新实现。

## Solution

单进程全自动交易系统（`polymarket-kronos`，独立轻量实现，不依赖 `ariesljm/Kronos` 引擎进程）：

```
Binance 15m K线 ──→ Kronos 模型推理（本地 CPU，shiyu-coder model 包）
                        │  P(up) 概率
                        ▼
              策略决策引擎 decide(state, market) → Action
                        │
                        ▼
              Polymarket CLOB（py-clob-client-v2，现有钱包）
                        │
                        ▼
        本地日志 + status.json（熔断状态）+ 交易统计
```

所有策略参数集中在 `config.yaml`，改参数不动代码。

## User Stories

1. 作为交易者，我想让系统在 BTC 15m 窗口开盘后自动用最新闭合 K 线预测下一窗口方向，以便及时参与。
2. 作为交易者，我想让系统只在 P(up)>60% 时买 Up、P(up)<40% 时买 Down，以便跳过模糊信号。
3. 作为交易者，我想让系统以 0.45 限价单等价格回调入场，以便低价买入降低成本。
4. 作为交易者，我想让系统在窗口结束前 3 分钟仍未成交时撤单放弃该窗口，以便资金不滞留。
5. 作为交易者，我想让系统在买入后价格涨到 0.80 时自动卖出，以便锁定利润。
6. 作为交易者，我想让系统在价格跌破 0.30 或窗口过 10 分钟仍未盈利时止损卖出，以便控制亏损。
7. 作为交易者，我想让系统每笔固定下注 1 USDC（可配置），以便风险可控。
8. 作为交易者，我想让系统在连续亏损 10 笔或单日亏损 10 USDC 时自动暂停（熔断），以便防止失控，等待人工恢复。
9. 作为交易者，我想让系统记录每笔交易与所有事件到本地日志，以便复盘。
10. 作为交易者，我想让系统把运行/熔断状态写入 status.json，以便我随时查看。
11. 作为交易者，我想让系统统计胜率、ROI、Kronos 方向准确率三个指标，以便判断验证结果（≥7 天 / ≥200 笔）。
12. 作为交易者，我想在 config.yaml 中修改所有策略参数（阈值、价格、金额、熔断线等），以便不用改代码即可调优。
13. 作为交易者，我想让系统在启动时校验配置合法性并给出明确报错，以便避免错误参数上线。
14. 作为交易者，我想让系统用我的现有钱包（私钥存 .env，不进仓库）接入 Polymarket，以便实盘。
15. 作为交易者，我想让系统标的（BTC/ETH）只靠配置切换，以便验证通过后扩展。
16. 作为交易者，我想让系统在数据源/预测/市场查询失败时跳过该窗口并记录错误继续运行，以便系统健壮。
17. 作为交易者，我想让系统在熔断后由我手动恢复并继续按配置运行，以便人工把关。
18. 作为交易者，我想让系统预留 Telegram 通知接口（默认关闭），以便以后长期挂机时接入。

## Implementation Decisions

- **模块划分**（单进程，职责分离）：
  1. 配置模块 — 读 `config.yaml` + 环境变量（`.env`），启动时校验合法性
  2. 数据源适配器 — Binance 15m K 线（ccxt），**首次全量回填 + 之后每窗口增量拉取，落 CSV 滚动存储**（每标的一个 `data/<symbol>_15m.csv`，列 `timestamp,open,high,low,close,volume`；超过 `max_klines`（默认 2048，即 kronos-mini 上下文上限）裁剪头部；按 CSV 末尾 timestamp 增量）；接口可注入 fake
  3. Kronos 预测模块 — 引入 shiyu-coder `model` 包，`KronosPredictor` 多路径采样（sample_count 可配置，默认 20）计算 `P(up)` = 预测涨的路径占比；模型变体可配置（默认 kronos-mini，CPU 推理）；Kronos 方向准确率在此单独记录（预测方向 vs 实际方向，与交易结果解耦）
  4. 市场发现模块 — gamma-api 查询当前 BTC 15m up/down 市场（`acceptingOrders`、entry 时间线、token_id），可注入 fake；**参考 LuciferForge/polymarket-btc-autotrader 的 scanner.py 逻辑**：`outcomePrices`/`outcomes`/`clobTokenIds` 为 JSON 编码字符串需解析；过滤条件 active+!closed+acceptingOrders+双结果；CLOB orderbook 排序陷阱（bids 升序/asks 降序，best bid=max、best ask=min）；`outcomePrices` 为最后成交价，可执行价需查 orderbook best ask
  5. **策略决策引擎** — 纯函数 `decide(state, market) → Action`，所有策略参数从配置注入，不写死数字
  5a. **策略抽象** — `Strategy` 接口：`generate_signal(data_context) → Signal{direction: up|down|skip, p_up}`；策略通过工厂按配置实例化（`strategy: kronos`），策略参数在 config 中按策略名分节；**Kronos 策略是第一个实现，后续可插拔切换（动量/SNIPE/其他模型）**；执行层（挂单/撤单/止盈/止损/熔断）由所有方向性策略共享
  6. 执行适配器 — py-clob-client-v2：挂限价单/撤单/卖出，可注入 fake
  7. 时钟适配器 — 可注入 fake 时间（测试用）
  8. 状态与统计模块 — 持仓跟踪、熔断计数（连亏/日亏）、status.json、交易日志、指标统计
- **Action 枚举**：`PLACE_LIMIT`（方向+价格+金额）/ `CANCEL` / `SELL`（原因：take_profit | stop_loss | time_stop）/ `SKIP` / `PAUSE`
- **认证**：钱包私钥在 `.env`（L1 派生 API key → L2 creds），chain_id=137（Polygon 主网）；Amoy 测试网（80002）作为可选验证通道
- **策略参数（config.yaml 示例）**：`symbols: [BTC]`、`amount_per_trade: 1`、`p_up_buy: 0.60`、`p_down_sell: 0.40`、`limit_price: 0.45`、`cancel_before_end_sec: 180`、`take_profit: 0.80`、`stop_loss: 0.30`、`time_stop_min: 10`、`max_consecutive_losses: 10`、`max_daily_loss: 10`、`model_variant: kronos-mini`、`sample_count: 20`、`telegram_enabled: false`（接口预留）
- **阶段化**：阶段 1 仅 BTC；阶段 2 配置加 ETH。每窗口每标的最多一注。
- **参考项目**（票 03 实现时）：LuciferForge/polymarket-btc-autotrader（scanner.py 市场扫描 / executor.py 下单 / risk_governor.py 风控 / portfolio.py 持仓），其 SDK 用 v1（已归档）仅作逻辑参考，本项目用 py-clob-client-v2。
- **数据存储选型**：CSV 滚动更新（而非 DuckDB/SQLite）——数据量上限 2048 根 K 线（Kronos 模型上下文上限，mini=2048/small+base=512），单文件约 100KB，顺序读写即可，无需数据库；`max_klines` 可配置。
- **关键外部依赖待核实**（实施时）：Polymarket 2025-09 改版影响；加密 up/down 市场费率（约 0.5% 传闻）；结算 tie/取秒规则；现有钱包（Polymarket 托管？）能否导出私钥。

## Testing Decisions

- **好测试的标准**：只测外部行为——给定 (状态, 市场输入, 配置) 期望得到确定的 Action，不测内部实现细节。
- **唯一接缝**：策略决策引擎 `decide`。数据源/执行/时钟均注入 fake，测试不碰网络、不碰真钱、不依赖真实时间。
- **必测场景**：
  - 信号阈值边界（P(up)=60% 恰好买入、59.9% 跳过、40%/40.1% 对称）
  - 挂单后价格回调成交路径；未成交在窗口结束前 3 分钟触发撤单
  - 止盈触发（价格 ≥0.80 → SELL take_profit）
  - 止损触发（价格 ≤0.30 → SELL stop_loss；窗口过 10 分钟未盈利 → SELL time_stop）
  - 熔断（连亏 10 笔 → PAUSE；单日亏 10 USDC → PAUSE；恢复需手动）
  - 配置校验（非法参数启动报错）
  - 每窗口每标的最多一注（不重复下单）
- **集成冒烟（只读）**：真实调用 gamma-api 查询一次当前 BTC 15m 市场，验证市场发现模块（不产生订单）。

## Out of Scope

- VPS 部署与 24/7 运维（验证通过后）
- Telegram 通知实际接入（只留接口，默认关闭）
- ETH 及多标的并行（阶段 2）
- 历史回测引擎
- Kronos 模型微调
- SNIPE / ARB / 网格等复杂策略
- Polymarket KYC 与充值流程（用户自行处理）

## Further Notes

- **市场现状（2026-08-14 实查 gamma-api）**：BTC/ETH/SOL/XRP/HYPE/BNB/DOGE 均有 Up or Down 市场，档位 5m/15m/1h/4h/Daily（BTC 确认有 15m 档）；**slug 模式 `btc-updown-15m-<窗口起始Unix秒>`**（15m 对齐，7×24 连续）；结算规则：窗口内 Chainlink BTC/USD TWAP ≥ 窗口开始价 → Up，否则 Down（非 Pyth，spec 原文中"Pyth 现货价"已修正）；BTC 15m 市场流动性约 $14k。
- 止盈 0.80 触发率可能偏低（15m 窗口价格通常在末段才到高位），验证后按实际数据调参。
- "Kronos 方向准确率" 与交易盈亏分开统计——交易亏可能只是参数问题，方向准确率单独暴露模型能力。
- 参考实现：LuciferForge/polymarket-btc-autotrader（BTC/SOL 15m 自动交易架构参考）；官方 SDK 现状：py-sdk（推荐新项目）/ py-clob-client-v2（活跃）/ v1（已归档勿用）。
