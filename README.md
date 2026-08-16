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
| `uv run python -m pmbot.monitor` | 只读监控面板（独立进程） |
| `uv run python -m pmbot.predict_cli` | 跑一次真实预测，输出信号与方向准确率 |
| `uv run python -m pmbot.report` | 生成验证报告（data/trades.csv + 预测准确率） |
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
tests/                测试（234 个，pytest）
config.yaml           全部策略参数
data/                 运行时数据（status.json / trades.csv / 凭证缓存）
models/               模型权重（git 忽略）
logs/backtest/        历史回测日志
docs/                 ADR、agent 文档、参考资料
```

## 安全说明

- **默认 dry-run**：忘记传参也不碰真钱（`run.py` 安全默认）
- 熔断：连亏 N 笔或单日亏 N USDC 自动暂停；人工编辑 `data/status.json` 的 `paused: false` 恢复
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
