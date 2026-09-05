# polymarket-kronos

Polymarket 加密货币涨跌（Up/Down）策略交易框架：**策略 → 决策引擎 → CLOB 下单**。5m/15m/1h 窗口对齐，`strategy:` 顶层字段切换策略，dry-run 默认安全模式。

> 领域模型与术语表见 [CONTEXT.md](CONTEXT.md)；架构决策见 [docs/adr/](docs/adr/)。

## 快速开始

```bash
# 安装依赖（Python ≥ 3.14；已移除 Kronos 时代重型依赖）
uv sync

# 模拟运行（默认 dry-run，不碰真钱）
uv run python -m pmbot.start_bot --dry-run
# 实盘（真钱！需 .env 配置 PRIVATE_KEY / PROXY_WALLET）
uv run python -m pmbot.start_bot --live
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `uv run python -m pmbot.start_bot [--dry-run\|--live]` | 一键启动：主循环（后台）+ 监控面板（前台） |
| `uv run python -m pmbot.run [--dry-run\|--live] --symbol BTC --data-dir data_multi/btc` | 单标的专用主循环（多标的并行各跑一个进程） |
| `uv run python -m pmbot.monitor [--web-port 8765]` | 监控面板 + Web 控制台（http://127.0.0.1:8765，仅本机） |
| `uv run python scripts/multi_panel.py [--data-dir data_multi/btc,data_multi/eth,data_multi/sol]` | 多标的聚合终端面板（视图复用 build_multi_view） |
| `start_multi.bat` / `start_momentum.bat` | 多标的 / 单标的批量启动脚本 |
| `uv run python -m pmbot.report` | 验证报告（trades.csv + 策略统计；实盘用 `--data-dir data_live`） |
| `uv run pytest` | 跑全部测试（423 个） |

## 策略切换（框架核心）

- `config.yaml` 顶层 `strategy: momentum` 选中策略；策略专属参数放同名分节（`momentum:`）。
- 新增策略：`src/pmbot/strategies/` 写策略类 + `@register("名字")`，在 `strategies/__init__.py` 加一行 import——**config.py 无需改动**（策略名单由 registry 推导）。
- 构造前依赖注入按策略类声明（如 momentum 的 `needs_fetch_price = True` 触发 Binance 实时价 WS 注入），run.py 不再按策略名硬编码。

当前策略：

- **momentum** — Binance 价格相对窗口开盘穿越阈值（±0.08%）后同向入场；入场价上限 `max_entry_price` 防追高。研究背景见 `docs/reports/momentum_strategy.md`。

## 目录结构

```
src/pmbot/            核心代码
  strategies/         Strategy 实现（momentum）与注册入口
  strategy.py         策略 ABC + registry（create_strategy/strategies）
  engine.py           决策引擎（纯函数，无 IO，接 EngineConfig 窄视图）
  main_loop.py        主循环编排（TradingLoop，纯编排器）
  execution_dispatcher.py 执行分派器（动作执行/挂单成交检测/平仓）
  market_lifecycle.py 单窗口生命周期状态机（LifecycleDeps 窄接口）
  executor_protocols.py 执行器窄接口 Protocols
  clob_executor.py    CLOB 下单/撤单/盘口（实盘/模拟适配器）
  panel_view.py       展示视图构建（build_view/build_live_view/build_multi_view）
  spot_ticker.py      Binance 实时价 WS 线程（动量穿越检测用）
  book_sampler.py     盘口采样（WS + REST 兜底 + 落盘）
  wallet.py           余额/持仓核对（WalletView 窄接口）
  settler.py          结算状态机（tick 一行调用，规则独立可测）
  ledger.py           交易账本统一读面（schema/配对/判据单一事实源）
  data_source.py      Binance K 线拉取与 CSV 滚动存储
  fileio.py           原子写工具（状态文件落盘统一入口）
  ...
tests/                测试（423 个，pytest）
config.yaml           唯一配置源（顶层通用参数 + 策略分节）
data/                 模拟模式（dry-run）运行时数据（status.json / trades.csv / 凭证缓存）
data_multi/       多标的并行数据（每标的一个子目录: btc/ eth/ sol/；--data-dir 指定，git 忽略）
data_live/            实盘模式运行时数据（--live 自动使用，与模拟完全分离）
docs/                 ADR、agent 文档、研究报告
```

## Web 控制台

监控面板内嵌 HTTP 控制台（默认 http://127.0.0.1:8765，仅本机可访问，`--web-port -1` 禁用）：

- **状态**：标的 / 主循环运行状态 / 窗口 / 信号 / 持仓 / 盘口 / 交易历史（全量）
- **控制**：启动（拉起主循环，`--live/--dry-run` 与面板一致）、停止（优雅停机：撤单→结算→落盘）、恢复运行（解除熔断并清零计数）、清除数据（重建状态并清空记录）。**有持仓时清除会丢弃持仓跟踪**（Polymarket 结算自动兑付，不丢资金）

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
- 单一事实源（禁止本地复刻）：止盈止损公式 `exit_rules.py`、盘口定价 `book_price.py`、交易账本 schema/读面 `ledger.py`、配置派生 `config.py`（`_ENGINE_FIELDS` 白名单）、K 线拉取 `data_source.fetch_klines_batch`
- 依赖声明与实际 import 一一对应（已移除 torch/einops/safetensors/huggingface-hub/scipy 等 Kronos 遗留）