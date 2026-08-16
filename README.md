# polymarket-kronos

Polymarket 加密货币涨跌（Up/Down）预测交易机器人：Kronos 模型推理信号 → 决策引擎 → CLOB 下单。5m/15m/1h 窗口对齐，dry-run 默认安全模式。

> 领域模型与术语表见 [CONTEXT.md](CONTEXT.md)；架构决策见 [docs/adr/](docs/adr/)。

## 快速开始

```bash
# 安装依赖（Python ≥ 3.14）
uv sync

# 配置（策略参数全部在 config.yaml，改参数无需改代码）
# 模型权重首次运行自动下载到 models/（也可手动断点续传下载）
uv run python -m pmbot.download_model kronos-base

# 模拟运行（默认 dry-run，不碰真钱）
uv run python -m pmbot.start_bot --dry-run
# 实盘（真钱！需 .env 配置 PRIVATE_KEY / PROXY_WALLET）
uv run python -m pmbot.start_bot --live
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `uv run python -m pmbot.start_bot [--dry-run\|--live]` | 一键启动：主循环（后台）+ 监控面板（前台） |
| `uv run python -m pmbot.run [--dry-run\|--live] [--once]` | 仅主循环（调试可用 `--once` 跑一个 tick） |
| `uv run python -m pmbot.monitor [--web-port 8765]` | 监控面板 + Web 控制台（http://127.0.0.1:8765，仅本机） |
| `uv run python -m pmbot.predict_cli` | 跑一次真实预测，输出信号与方向准确率 |
| `uv run python -m pmbot.report` | 生成验证报告（trades.csv + 预测准确率；实盘用 `--data-dir data_live` 需另行指定，见 `report.py --help`） |
| `uv run python -m pmbot.backtest [--interval 15m]` | 离线方向准确率回测（15m vs 5m 对比） |
| `uv run python -m pmbot.backtest_sim` | 交易模拟回测（真实成交路径，限价 vs 市价） |
| `uv run python -m pmbot.download_model <variant>` | 模型权重断点续传下载 |

## 目录结构

```
src/pmbot/            核心代码
  strategies/         Strategy 实现（kronos）
  vendor_kronos/      上游 Kronos 模型 vendored 代码（勿改）
  engine.py           决策引擎（纯函数，无 IO）
  main_loop.py        主循环编排（TradingLoop）
  market_lifecycle.py 单窗口生命周期状态机
  predictor.py        Kronos 推理客户端
  clob_executor.py    CLOB 下单/撤单/盘口查询
  data_source.py      Binance K 线拉取（fetch_klines_batch）与 CSV 滚动存储
  fileio.py           原子写工具（状态文件落盘统一入口）
  ...
tests/                测试（280 个，pytest）
config.yaml           全部策略参数
data/                 模拟模式（dry-run）运行时数据（status.json / trades.csv / 凭证缓存）
data_live/            实盘模式运行时数据（`--live` 自动使用，与模拟完全分离）
models/               模型权重（git 忽略）
logs/backtest/        历史回测日志
docs/                 ADR、agent 文档、参考资料
```

## Web 控制台

监控面板内嵌 HTTP 控制台（默认 http://127.0.0.1:8765，仅本机可访问，`--web-port -1` 禁用）：

- **状态**：标的 / 主循环运行状态 / 窗口 / 信号 / 持仓 / 盘口 / 交易历史（全量）
- **控制**：启动（拉起主循环，`--live/--dry-run` 与面板一致）、停止（优雅停机：撤单→结算→落盘）、恢复运行（解除熔断并清零计数）、清除数据（重建状态并清空 K线/交易/预测记录）。**有持仓时清除会丢弃持仓跟踪**（Polymarket 结算自动兑付，不丢资金）；主循环停止时由面板直接清除，避免“停止后持仓不平仓 → 无法清除”的死锁

控制指令经 `data/control.json` 由主循环每 tick 消费（读后即删，无竞态）。

## 安全说明

- **默认 dry-run**：忘记传参也不碰真钱（`run.py` 安全默认）
- **模拟/实盘数据分离**：dry-run 用 `data/`，`--live` 自动用 `data_live/`；status.json 记录运行模式，跨模式启动会拒绝（防误用）
- 熔断：连亏 N 笔或单日亏 N USDC 自动暂停；面板 Web 控制台「恢复运行」一键解除（也可人工编辑 `data/status.json` 的 `paused: false` 后重启）
- `data/clob_creds.json` 含 CLOB API 凭证（git 忽略），勿提交
- 私钥/代理钱包从 `.env` 读取（`PRIVATE_KEY` / `PROXY_WALLET`）

## 开发

```bash
uv run pytest          # 跑全部测试
```

- 单实例守护：`start_bot` / `run` 双入口互斥，防多开互踩状态文件
- 新 WS 流继承 `ws_thread.ReconnectingWsThread` 骨架，勿复制样板
- 状态文件落盘统一走 `fileio.atomic_write_text`（tmp+replace 防半写）
- 止盈止损公式（`exit_rules.py`）、盘口定价（`book_price.py`）、模型变体（`variant_map.py`）、K 线拉取（`data_source.fetch_klines_batch`）为单一事实源，禁止本地复刻
