"""Polymarket 独立数据记录器：实时盘口/价格/Binance + 每窗口结算。

目的:为策略讨论积累「真实、活性可验证」的市场数据——吸取冻结假象教训
(旧 record_join_ws 读 bot 的 book.json,bot 停机时价格冻结、时间戳照走,
审计由此伪造出「低估价低估」)。本记录器**自己拉** gamma/CLOB/Binance,
不依赖 bot 存活;每行快照带 timestamp,价格变化即活性证据。

数据落盘(data/market_rec/):
- snapshots_YYYYMMDD.jsonl   每 5 秒一行:{ts, window_start, up/down 盘口前 5 档, binance}
- settlements.jsonl          每窗口结束 ~15s 一行:{window_start, up_wins, 结算价}

用法(后台): HTTPS_PROXY=... HTTP_PROXY=... nohup uv run python scripts/record_market.py &
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
STEP = 300          # 5m 窗口
POLL_SEC = 5.0       # 快照间隔
SETTLE_DELAY = 15    # 窗口结束后 N 秒拉结算
MAX_DEPTH = 5        # 每侧记录档数
DATA_DIR = Path("data") / "market_rec"


def log_setup() -> logging.Logger:
    logger = logging.getLogger("market_rec")
    logger.setLevel(logging.INFO)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler("logs" / Path("data_recorder.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    return logger


def single_instance() -> bool:
    """pid 文件防重复启动;成功则写入自身 pid。"""
    pid_file = DATA_DIR / "recorder.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # 进程存活则拒绝再启
            return False
        except (OSError, ValueError):
            pass  # 已死/无法校验,允许覆盖
    pid_file.write_text(str(os.getpid()))
    return True


def now() -> int:
    return int(time.time())


def get(url: str, params: dict | None = None, timeout: int = 15, **kw) -> requests.Response:
    return requests.get(url, params=params, headers=UA, timeout=timeout, **kw)


def window_tokens(w: int) -> dict | None:
    """gamma 拉窗口事件 → {up_token, down_token, question}。"""
    r = get(f"{GAMMA}/events", params={"slug": f"btc-updown-5m-{w}"})
    events = r.json()
    m = events[0]["markets"][0]
    toks = json.loads(m["clobTokenIds"])
    return {"up": toks[0], "down": toks[1], "question": m.get("question", "")}


def book_snapshot(token_id: str) -> dict | None:
    """"CLOB book: {bids:[[p,s]…], asks:[[p,s]…]} 前 MAX_DEPTH 档;失败 None。"""
    r = get(f"{CLOB}/book", params={"token_id": token_id})
    d = r.json()
    def first(rows):
        return float(rows[0]["price"]) if rows else None
    def top(rows):
        return [[float(x["price"]), float(x["size"])] for x in rows[:MAX_DEPTH]]
    return {
        "bid": first(d.get("bids") or []),
        "ask": first(d.get("asks") or []),
        "bids": top(d.get("bids") or []),
        "asks": top(d.get("asks") or []),
    }


def binance_price() -> float | None:
    """Binance 镜像直连(无代理,大陆可达)。"""
    try:
        r = requests.get(BINANCE, timeout=5, proxies={"http": None, "https": None})
        return float(r.json()["price"])
    except Exception:
        return None


def settle(w: int, up_token: str) -> dict:
    """"窗口结算:up_wins + 双侧结算价(结算后 outcomePrices 为 0/1)。"""
    try:
        r = get(f"{GAMMA}/events", params={"slug": f"btc-updown-5m-{w}"})
        m = r.json()[0]["markets"][0]
        outs = json.loads(m["outcomes"])
        ops = json.loads(m["outcomePrices"])
        prices = dict(zip(outs, ops))
        return {
            "window_start": w,
            "up_wins": int(prices.get("Up") == "1"),
            "up_price": prices.get("Up"),
            "down_price": prices.get("Down"),
            "ts": now(),
        }
    except Exception as e:
        return {"window_start": w, "up_wins": None, "error": str(e), "ts": now()}


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (Path("logs")).mkdir(exist_ok=True)
    logger = log_setup()
    if not single_instance():
        logger.error("已有实例在运行,退出")
        sys.exit(1)

    day = time.strftime("%Y%m%d")
    snap_path = DATA_DIR / f"snapshots_{day}.jsonl"
    settle_path = DATA_DIR / "settlements.jsonl"
    logger.info("记录器启动 pid=%s → %s", os.getpid(), DATA_DIR)

    cur_w, tokens = None, None
    settled: set[int] = set()
    # 启动即入快照循环;掉线计数
    errors = 0
    while True:
        try:
            w = now() // STEP * STEP
            if w != cur_w:
                # 窗口切换:token 缓存刷新(新窗口);结算处理放窗口结束判定
                try:
                    tokens = window_tokens(w)
                    cur_w = w
                    errors = 0
                    settle_path.open("a", encoding="utf-8").write(
                        json.dumps({"event": "new_window", "window_start": w,
                                    "question": tokens.get("question")}) + "\n")
                except Exception as e:
                    logger.warning("拉窗口 %d token 失败: %s", w, e)
                    time.sleep(POLL_SEC)
                    continue

            # 结算:上一窗口结束后拉一次
            if tokens is not None:
                prev_w = w - STEP
                if prev_w not in settled and now() >= prev_w + STEP + SETTLE_DELAY:
                    try:
                        s = settle(prev_w, tokens["up"])
                        settle_path.open("a", encoding="utf-8").write(
                            json.dumps(s, ensure_ascii=False) + "\n")
                        settled.add(prev_w)
                        if s.get("up_wins") is not None:
                            logger.info("窗口 %d 结算: up_wins=%s", prev_w, s["up_wins"])
                    except Exception as e:
                        logger.warning("窗口 %d 结算失败: %s", prev_w, e)

            # 快照
            snap = {"ts": now(), "window_start": w, "binance": binance_price()}
            all_ok = True
            if tokens is not None:
                for side, tok in (("up", tokens["up"]), ("down", tokens["down"])):
                    try:
                        snap[side] = book_snapshot(tok)
                    except Exception as e:
                        snap[side] = None
                        all_ok = False
                        logger.warning("盘口 %s 拉取失败: %s", side, e)
            snap["ok"] = all_ok
            with snap_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(snap) + "\n")

            if len(settled) % 12 == 1:
                logger.info("运行中: 快照 %s, 已结算窗口 %s/%s", snap_path.name, len(settled),
                            (now() - int(os.path.getmtime(snap_path))) // STEP if snap_path.exists() else 0)
        except Exception as e:
            errors += 1
            logger.error("主循环异常(#%d): %s", errors, e)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()