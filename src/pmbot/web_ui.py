"""Web 控制台：monitor 进程内嵌 HTTP 服务（标准库，零新依赖）。

- GET  /            控制台页面（HTML/JS，轮询 /api/status）
- GET  /api/status  状态 JSON（复用 monitor.build_view，交易历史全量）
- POST /api/control {cmd: resume|reset|stop|start}
- POST /api/ui      {show: bool} 切换终端面板显隐（web-only 模式）

控制指令经 control.json 由主循环消费；start 为进程级操作，本模块直接
拉起 run.py 子进程（single_instance 守护防多开）。仅绑定 127.0.0.1。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from pmbot.control import write_control
from pmbot.single_instance import InstanceGuard

WEB_PORT = 8765
PID_FILE = "data/bot.pids"


from pmbot.paths import ProcessControl


def loop_alive(pid_file: str | Path = PID_FILE) -> bool:
    """主循环（run 角色）进程是否存活（读 bot.pids + 跨平台存活检查）。"""
    try:
        data = json.loads(Path(pid_file).read_text(encoding="utf-8"))
        pid = data.get("run")
        if not pid:
            return False
        return InstanceGuard.alive(int(pid))
    except Exception:
        return False


def spawn_loop(config: str, live: bool, data_dir: str = "data") -> subprocess.Popen:
    """拉起主循环子进程（输出进 logs/bot.log，与 start_bot 同一路径）。"""
    mode = "--live" if live else "--dry-run"
    os.makedirs("logs", exist_ok=True)
    logf = open(Path("logs") / "bot.log", "a", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "pmbot.run", mode, "--config", config,
         "--data-dir", data_dir],
        stdout=logf,
        stderr=subprocess.STDOUT,
    )


def make_handler(
    view_fn: Callable[[], dict],
    config: str,
    live: bool,
    holder: "ProcessControl",
    lock: threading.Lock,
    status_path: str = "data/status.json",
    trades_path: str = "data/trades.csv",
    log_dir: str = "data",
    data_dir: str = "data",
    pid_file: str | Path = PID_FILE,
    set_mode_fn: Callable[[bool], None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """构造 HTTP handler（闭包注入共享状态）。

    set_mode_fn: 模拟/实盘切换回调（monitor 注入，更新面板与启动参数）。
    模式与数据目录动态值从 holder（ProcessControl）读取（切换后 start
    拉起新进程用新模式）。
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass  # 面板已有日志，HTTP 访问不刷屏

        def _send_json(self, code: int, data: dict) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self) -> None:
            body = WEB_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/index"):
                self._send_html()
            elif self.path == "/api/status":
                try:
                    v = view_fn()
                    if not isinstance(v, dict):
                        from dataclasses import asdict

                        v = asdict(v)  # PanelView → JSON（字段名即键名，唯一出处）
                    v["loop_alive"] = loop_alive(pid_file)
                    v["show_tui"] = holder.show_tui
                    self._send_json(200, v)
                except Exception as e:  # 只读路径：异常返回 500，不崩溃
                    self._send_json(500, {"ok": False, "error": str(e)})
            else:
                self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad request"})
                return
            if self.path == "/api/ui":
                show = body.get("show")
                if not isinstance(show, bool):
                    self._send_json(400, {"ok": False, "error": "show 必须为布尔值"})
                    return
                holder.show_tui = show
                self._send_json(200, {"ok": True, "show_tui": show})
                return
            if self.path == "/api/mode":
                new_live = body.get("live")
                if not isinstance(new_live, bool):
                    self._send_json(400, {"ok": False, "error": "live 必须为布尔值"})
                    return
                if loop_alive(pid_file):
                    self._send_json(409, {"ok": False, "error": "主循环运行中，请先停止再切换模式"})
                    return
                if set_mode_fn is None:
                    self._send_json(500, {"ok": False, "error": "模式切换未启用"})
                    return
                set_mode_fn(new_live)
                self._send_json(200, {"ok": True, "live": new_live})
                return
            if self.path != "/api/control":
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            cmd = str(body.get("cmd", ""))
            if cmd == "start":
                with lock:
                    if loop_alive(pid_file):
                        self._send_json(409, {"ok": False, "error": "主循环已在运行"})
                        return
                    holder.proc = spawn_loop(config, holder.live, holder.paths.data_dir)
                self._send_json(200, {"ok": True, "started": True})
            elif cmd == "reset":
                if holder.live:
                    self._send_json(409, {"ok": False, "error": "实盘模式禁止清除数据（保护交易历史与持仓管理）"})
                    return
                if loop_alive(pid_file):
                    write_control(cmd, Path(data_dir) / "control.json")  # 主循环消费（丢弃持仓跟踪）
                else:
                    # 主循环已停止：面板直接清数据文件，避免“停止后持仓不平仓 → 永远无法清除”死锁
                    _reset_files(status_path, trades_path, log_dir, config)
                self._send_json(200, {"ok": True})
            elif cmd in ("resume", "stop"):
                try:
                    write_control(cmd, Path(data_dir) / "control.json")
                except ValueError as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                    return
                self._send_json(200, {"ok": True})
            else:
                self._send_json(400, {"ok": False, "error": f"未知指令: {cmd}"})

    return Handler


def _reset_files(status_path: str, trades_path: str, log_dir: str, config: str) -> None:
    """主循环未运行时直接清除数据文件（symbol/interval 从状态与配置读取）。"""
    import json as _json

    from pmbot.config import load_config
    from pmbot.control import clear_data_files

    symbol = ""
    try:
        symbol = str(_json.loads(Path(status_path).read_text(encoding="utf-8")).get("symbol", ""))
    except Exception:
        pass
    interval = "15m"
    try:
        interval = load_config(config).market_interval
    except Exception:
        pass
    clear_data_files(status_path, trades_path, log_dir, symbol, interval)


def start_server(
    view_fn: Callable[[], dict],
    config: str,
    live: bool,
    port: int = WEB_PORT,
    holder: "ProcessControl | None" = None,
    lock: threading.Lock | None = None,
    status_path: str = "data/status.json",
    trades_path: str = "data/trades.csv",
    log_dir: str = "data",
    data_dir: str = "data",
    pid_file: str | Path = PID_FILE,
    set_mode_fn: Callable[[bool], None] | None = None,
) -> ThreadingHTTPServer:
    """启动 Web 控制台服务（daemon 线程，仅本机）。"""
    holder = holder if holder is not None else ProcessControl()
    lock = lock if lock is not None else threading.Lock()
    handler = make_handler(view_fn, config, live, holder, lock,
                           status_path, trades_path, log_dir, data_dir, pid_file,
                           set_mode_fn)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


WEB_HTML = (Path(__file__).with_name("console.html")).read_text(encoding="utf-8")
