"""Binance 秒级价 + Polymarket 盘口联合记录器（入场点标定的数据积累）。

背景：Kronos 信号只给方向，入场价 = 信号时点的盘口 ask，经常不是最佳价。
要验证「Binance 实时移动先于 Polymarket 盘口异动、能否择机入场」，
需要同一时刻的 Binance 秒级价与 Polymarket 盘口序列 —— 本项目此前只积累了
Binance 侧（calib_1s.csv），盘口侧从未落盘历史。本脚本补上这一半。

数据来源（零新增依赖，全部复用现有组件）：
- Binance：SpotTickerThread（WS miniTicker + REST 兜底，1s 级）
- Polymarket：读主循环 BookSampler 每 1s 原子落盘的 <data-dir>/book.json
  （bot 必须同时运行；bot 未跑时盘口列保持空）

输出：<data-dir>/recordings/calib_book_1s.csv
  ts, binance_price, up_ask, up_bid, down_ask, down_bid
  （ts = 采集时刻 ms；窗口边界事后按 300s 对齐，无需在记录时感知）

数据目录自动探测：优先 --data-dir 显式指定；否则读 data_live/status.json 存在
且 mode=live → data_live（实盘），否则 data/（dry-run）；交易标的从 status.json
的 symbol 读取（实盘 ETH / dry-run BTC 均自动跟随）。

用法：
  # 终端 1：启动 bot（dry-run 或实盘均可，BookSampler 每秒落盘 book.json）
  uv run python -m pmbot.run --dry-run        # 或 --live
  # 终端 2：启动本记录器（自动跟随模式与标的）
  uv run python -m scripts.record_join_ws
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from pathlib import Path

from pmbot.spot_ticker import SpotTickerThread

logger = logging.getLogger(__name__)

COLUMNS = ["ts", "binance_price", "up_ask", "up_bid", "down_ask", "down_bid"]
POLL_SEC = 1.0


def detect_data_dir(explicit: str | None) -> str:
    """探测当前数据目录：显式指定优先；否则按已有 status.json 的模式跟随。

    实盘（data_live/status.json mode=live）→ data_live，其余 → data/。
    与 paths_for 的"跨模式启动拒绝"配合：记录器不必感知 mode 就用对盘口文件。
    """
    if explicit:
        return explicit
    try:
        st = json.loads(Path("data_live/status.json").read_text(encoding="utf-8"))
        if st.get("mode") == "live":
            return "data_live"
    except Exception:
        pass
    return "data"


def detect_symbol(data_dir: str) -> str:
    """从 status.json 读取交易标的（自动跟随实盘 ETH / dry-run BTC）。"""
    try:
        st = json.loads(Path(f"{data_dir}/status.json").read_text(encoding="utf-8"))
        sym = st.get("symbol")
        if sym:
            return sym
    except Exception:
        pass
    return "BTC"


def read_book(path: Path) -> dict:
    """读 BookSampler 落盘快照；缺文件/半写容忍（原子写由 BookSampler 保证）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=POLL_SEC, help="采样间隔秒（默认 1）")
    ap.add_argument("--data-dir", default=None, help="数据目录（默认自动探测：data_live/ vs data/）")
    args = ap.parse_args()

    data_dir = detect_data_dir(args.data_dir)
    symbol = detect_symbol(data_dir)
    book_path = Path(f"{data_dir}/book.json")
    out_path = Path(f"{data_dir}/recordings/calib_book_1s.csv")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not out_path.is_file()
    fout = open(out_path, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fout, fieldnames=COLUMNS)
    if new_file:
        writer.writeheader()
        fout.flush()

    ticker = SpotTickerThread(symbol=symbol)
    ticker.start()
    logger.info("记录开始: data_dir=%s symbol=%s book=%s -> %s（Ctrl+C 停止）",
                data_dir, symbol, book_path, out_path)

    written = 0
    t_last_live = time.time()
    try:
        while True:
            b = read_book(book_path)
            row = {
                "ts": int(time.time() * 1000),
                "binance_price": ticker.latest_price(),
            }
            if b:
                row["up_ask"] = b.get("up_ask")
                row["up_bid"] = b.get("up_bid")
                row["down_ask"] = b.get("down_ask")
                row["down_bid"] = b.get("down_bid")
                t_last_live = time.time()
            else:
                # bot 未运行/窗口间空档：盘口列留空
                for k in ("up_ask", "up_bid", "down_ask", "down_bid"):
                    row.setdefault(k, None)
            writer.writerow(row)
            fout.flush()  # 逐行落盘：随时被杀不丢已采样行（1s 一次开销可忽略）
            written += 1
            if written % 60 == 0:
                age = time.time() - t_last_live
                logger.info("已记录 %s 行，盘口数据新鲜（%ds 前更新）", written, int(age))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        fout.flush()
        fout.close()
        ticker.stop()
        logger.info("记录停止，共 %s 行 -> %s", written, out_path)
    return 0


if __name__ == "__main__":
    import logging as _l

    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())