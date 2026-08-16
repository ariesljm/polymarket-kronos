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
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from pmbot.control import write_control
WEB_PORT = 8765

from pmbot.paths import ProcessControl


def _is_loopback(addr: str) -> bool:
    """本机回环地址（127.0.0.1 / ::1 / localhost）：本机访问免 token。"""
    return addr in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def _authorized(client_addr: str, sent_token: str, token: str | None) -> bool:
    """写操作鉴权：未配置 token 拒绝一切（安全默认）；

    已配置 token 时本机回环访问免 token（单机使用免 ?token= 负担），
    局域网访问必须携带正确 token。
    """
    if not token:
        return False
    if _is_loopback(client_addr):
        return True
    import secrets

    return bool(sent_token) and secrets.compare_digest(sent_token, token)


def make_handler(
    view_fn: Callable[[], dict],
    config: str,
    live: bool,
    holder: "ProcessControl",
    lock: threading.Lock,
    token: str | None = None,
    set_mode_fn: Callable[[bool], None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """构造 HTTP handler（闭包注入共享状态）。

    token: 写操作（POST /api/*）的访问令牌；None 时拒绝一切写操作
    （安全默认）。只读（页面/状态轮询）免令牌，便于局域网直接查看。
    set_mode_fn: 模拟/实盘切换回调（monitor 注入，更新面板与启动参数）。
    模式与数据目录动态值从 holder（ProcessControl）读取（切换后 start
    拉起新进程用新模式，stop/resume/reset 写入新模式的路径）。
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
                    v["data_dir"] = holder.paths.data_dir  # 数据目录（模拟 data/ 与实盘 data_live/ 各自独立）
                    v["loop_alive"] = holder.loop_alive()
                    v["show_tui"] = holder.show_tui
                    self._send_json(200, v)
                except Exception as e:  # 只读路径：异常返回 500，不崩溃
                    self._send_json(500, {"ok": False, "error": str(e)})
            else:
                # 未知路径（如 /start、/console）→ 重定向首页，避免用户误输路径后看到 404
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()

        def do_POST(self) -> None:
            # 写操作鉴权：本机回环免 token；局域网需 X-PMBOT-Token（与页面 URL ?token= 对应）
            if not _authorized(self.client_address[0],
                               self.headers.get("X-PMBOT-Token", ""), token):
                self._send_json(401, {"ok": False, "error": "未授权：本机访问免 token；局域网访问需 URL 加 ?token=xxx（见启动日志）"})
                return
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
                if holder.loop_alive():
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
                    if holder.loop_alive():
                        self._send_json(409, {"ok": False, "error": "主循环已在运行"})
                        return
                    holder.spawn(config)
                self._send_json(200, {"ok": True, "started": True})
            elif cmd == "reset":
                if holder.live:
                    self._send_json(409, {"ok": False, "error": "实盘模式禁止清除数据（保护交易历史与持仓管理）"})
                    return
                if holder.loop_alive():
                    write_control(cmd, Path(holder.paths.data_dir) / "control.json")  # 主循环消费（丢弃持仓跟踪）
                else:
                    # 主循环已停止：面板直接清数据文件，避免“停止后持仓不平仓 → 永远无法清除”死锁
                    _reset_files(holder.paths.status, holder.paths.trades, holder.paths.log_dir, config)
                self._send_json(200, {"ok": True})
            elif cmd in ("resume", "stop"):
                try:
                    write_control(cmd, Path(holder.paths.data_dir) / "control.json")
                except ValueError as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                    return
                self._send_json(200, {"ok": True})
            else:
                self._send_json(400, {"ok": False, "error": f"未知指令: {cmd}"})

    return Handler


def _reset_files(status_path: str, trades_path: str, log_dir: str, config: str) -> None:
    """主循环未运行时直接清除数据文件（symbol/interval 从状态与配置读取）。

    与主循环内 reset 同一条路径（control.reset_runtime）：删除清单不分裂。
    """
    from pmbot.control import reset_runtime

    reset_runtime(status_path, trades_path, log_dir, config=config)


def start_server(
    view_fn: Callable[[], dict],
    config: str,
    live: bool,
    port: int = WEB_PORT,
    host: str = "0.0.0.0",
    holder: "ProcessControl | None" = None,
    lock: threading.Lock | None = None,
    token: str | None = None,
    set_mode_fn: Callable[[bool], None] | None = None,
) -> ThreadingHTTPServer:
    """启动 Web 控制台服务（daemon 线程）。

    host 默认 0.0.0.0（局域网可访问；写操作需 token，只读公开）。
    路径单一事实源：holder.paths（切换模式后全部跟随新模式）。
    """
    holder = holder if holder is not None else ProcessControl()
    lock = lock if lock is not None else threading.Lock()
    handler = make_handler(view_fn, config, live, holder, lock, token,
                           set_mode_fn)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


WEB_HTML = (Path(__file__).with_name("console.html")).read_text(encoding="utf-8")
