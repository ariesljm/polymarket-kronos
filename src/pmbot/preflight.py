"""实盘前置自检：硬阻塞项不通过则拒绝启动（真钱操作的守门人）。

**硬阻塞项**（每一项都对应一种实盘必然失败）：
  1. 凭证缺失   — ClobExecutor 到下单时才抛错，应在启动期快速失败
  2. 余额为 0   — 下单全被服务端拒（2026-09-10 实测代理钱包 balance='0'）
  3. 授权未生效 — allowance=0，下单被拒（approve 可能被重置）
  4. 出口被地理限制 — Polymarket 按**下单来源 IP** 限制下单（官方文档：
     "restricts order placement from certain geographic locations ... on both the
     frontend and the API"），受限地区只能平仓不能开新仓。2026-09-10 实测本机
     节点出口是 AWS 东京，`/api/geoblock` 连续 15 次均 blocked=true。

**非阻塞提醒**（不拦启动，只提示）：
  - 每注金额换算出的股数低于 5 股：该下限只针对 **GTC/GTD 限价单**，市价单(FOK)
    不适用（实证：他人以 $1.05 成交 140+ 笔 1.2 股单，见 py-clob-client #301；
    5 股下限的拒单信息形如 "Size (1.08) lower than the minimum: 5"）。
    曾经把它误当硬规则拦下 $1 每注，故单独放这里。

调用方（live 启动前）::

    problems = live_preflight(cfg, ClobExecutor())
    if problems: 记录并拒绝启动
    for note in live_advisories(cfg): 记录提醒
"""

from __future__ import annotations

import os

from pmbot.executor_protocols import MIN_ORDER_SIZE

# 关闭入场价上限（max_entry_price=0）时无法推算最坏入场价，用常见上限兜底
_FALLBACK_CAP = 0.50

_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


def geoblock_status(timeout: int = 12) -> tuple[bool | None, str]:
    """查 bot **实际出口 IP** 的地理资格（Polymarket 官方 /api/geoblock）。

    返回 (blocked, 描述)；blocked=None 表示查询失败（无法确认，调用方降级处理）。
    走与实盘同一条代理路径（调用方已先 resolve_proxy() 写回环境），
    否则测到的不是下单时真正的出口。
    """
    import requests

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.get(_GEOBLOCK_URL, timeout=timeout, proxies=proxies)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    blocked = d.get("blocked")
    detail = f"country={d.get('country')} region={d.get('region')} ip={d.get('ip')}"
    return (bool(blocked) if blocked is not None else None), detail


def min_amount_for_rules(max_entry_price: float) -> float:
    """按 5 股下限折算的最低每注金额（仅适用于 GTC/GTD 限价单）。"""
    cap = max_entry_price if max_entry_price > 0 else _FALLBACK_CAP
    return MIN_ORDER_SIZE * cap


def live_preflight(
    cfg, executor, geo: tuple[bool | None, str] | None = None
) -> list[str]:
    """实盘硬阻塞项；返回问题清单（空列表 = 可启动）。不抛异常。

    geo: 地理资格查询结果；None = 现场查（测试注入用，避免真网络）。
    """
    problems: list[str] = []

    # 1. 凭证（ClobExecutor 构造时会 load_dotenv，故此处读到的即最终值）
    missing = [k for k in ("PRIVATE_KEY", "PROXY_WALLET") if not os.environ.get(k)]
    if missing:
        problems.append(f".env 缺少 {' / '.join(missing)}（实盘必需）")

    # 2. 余额 + 3. 授权（同源一次查询）
    balance: float | None = None
    allowances: dict = {}
    try:
        balance, allowances = executor.collateral_snapshot()
    except Exception as e:
        problems.append(f"余额/授权查询失败（凭证/代理/网络）：{type(e).__name__}: {e}")
    if balance is None and not problems:
        problems.append("无法读取代理钱包抵押余额（响应异常）")
    elif balance is not None:
        if balance <= 0:
            problems.append(f"抵押余额 {balance} pUSD，任何下单都会被服务端拒（请先充值）")
        if not allowances or all(float(v or 0) == 0 for v in allowances.values()):
            problems.append("抵押授权(allowance)为 0，下单会被拒（需 approve）")
        if balance > 0 and balance < cfg.amount_per_trade:
            problems.append(f"余额 {balance:.2f} 少于每注 {cfg.amount_per_trade:.2f} USDC")

    # 4. 地理资格（服务端按出口 IP 限制下单；受限地区只能平仓）
    blocked, geo_detail = geo if geo is not None else geoblock_status()
    if blocked is True:
        problems.append(
            f"出口被地理限制、禁止下新单：{geo_detail}（换一个非受限地区的节点后重试）"
        )
    elif blocked is None:
        problems.append(f"地理资格无法确认（服务端）：{geo_detail}")

    return problems


def live_advisories(
    cfg, geo: tuple[bool | None, str] | None = None
) -> list[str]:
    """非阻塞提醒（可启动）。"""
    notes: list[str] = []
    cap = cfg.max_entry_price if cfg.max_entry_price > 0 else _FALLBACK_CAP
    shares = cfg.amount_per_trade / cap if cap else 0.0
    if shares < MIN_ORDER_SIZE:
        notes.append(
            f"每注 {cfg.amount_per_trade} USDC 在入场上限 {cap} 时约 {shares:.1f} 股，低于 "
            f"GTC/GTD 的 {MIN_ORDER_SIZE} 股下限；市价单(FOK)不受此限，但若实盘出现 "
            f"\"Size ... lower than the minimum\" 拒单，请提高每注金额"
        )
    return notes
