"""Web 控制台测试：HTTP API（起真实 ThreadingHTTPServer，随机端口）。"""

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from pmbot.paths import ProcessControl, RuntimePaths, paths_for
from pmbot.web_ui import start_server


@pytest.fixture
def server(tmp_path):
    # 数据目录用 tmp：真实写控制指令（stop 等）不落仓库 data/，
    # 避免污染真实运行数据与 test_main_loop 的默认 control_path
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    holder = ProcessControl(paths=RuntimePaths(data_dir=str(tmp_path / "data")))
    lock = threading.Lock()

    def view_fn():
        return {"symbol": "ETH", "paused": False, "position": None,
                "recent_trades": [{"ts": "10:00", "direction": "up", "pnl": 1.0}]}

    srv = start_server(view_fn, "config.yaml", False, port=0, holder=holder, lock=lock,
                       token="test-token")
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}", holder
    srv.shutdown()
    srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def _post(url, body, token="test-token"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-PMBOT-Token"] = token
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_index_page(server):
    base, _ = server
    status, body = _get(base + "/")
    assert status == 200
    assert b"PMBOT" in body and b"PMBOT \xe6\x8e\xa7\xe5\x88\xb6\xe5\x8f\xb0" in body


def test_api_status(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: True)
    status, body = _get(base + "/api/status")
    assert status == 200
    data = json.loads(body)
    assert data["symbol"] == "ETH"
    assert data["loop_alive"] is True
    assert len(data["recent_trades"]) == 1


def test_api_control_writes_instruction(server, monkeypatch):
    base, holder = server
    calls = []
    monkeypatch.setattr("pmbot.web_ui.write_control",
                        lambda *a, **kw: calls.append(a[0]))
    monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: True)  # 运行中 → 走指令通道
    for cmd in ("resume", "reset", "stop"):
        status, data = _post(base + "/api/control", {"cmd": cmd})
        assert status == 200
        assert data["ok"] is True
    assert calls == ["resume", "reset", "stop"]


def test_api_control_unknown_cmd(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(base + "/api/control", {"cmd": "hack"})
    assert ei.value.code == 400


def test_api_control_start_spawns(server, monkeypatch):
    base, holder = server
    monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: False)

    class FakeProc:
        pid = 9999

        def poll(self):
            return None

    calls = []
    def fake_spawn(self, config):
        calls.append((config, self.live))
        self.proc = FakeProc()
        return self.proc

    monkeypatch.setattr("pmbot.paths.ProcessControl.spawn", fake_spawn)
    status, data = _post(base + "/api/control", {"cmd": "start"})
    assert status == 200
    assert data["started"] is True
    assert calls == [("config.yaml", False)]
    assert holder.proc is not None


def test_api_control_start_conflict(server, monkeypatch):
    base, holder = server
    monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: True)
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(base + "/api/control", {"cmd": "start"})
    assert ei.value.code == 409


def test_api_reset_when_loop_stopped_clears_files(tmp_path, monkeypatch):
    """主循环停止时 reset 由面板直接清除数据文件（解除死锁）。"""
    from pmbot.paths import ProcessControl, RuntimePaths
    from pmbot.web_ui import start_server

    (tmp_path / "status.json").write_text('{"symbol": "ETH"}', encoding="utf-8")
    (tmp_path / "trades.csv").write_text("ts\nx", encoding="utf-8")
    (tmp_path / "eth_5m.csv").write_text("t", encoding="utf-8")
    (tmp_path / "predictions_eth.csv").write_text("t", encoding="utf-8")
    monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: False)

    srv = start_server(lambda: {"symbol": "ETH"}, "config.yaml", False, port=0,
                       holder=ProcessControl(paths=RuntimePaths(data_dir=str(tmp_path))),
                       lock=threading.Lock(), token="test-token")
    port = srv.server_address[1]
    try:
        status, data = _post(f"http://127.0.0.1:{port}/api/control", {"cmd": "reset"})
        assert status == 200 and data["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()

    for name in ("status.json", "trades.csv", "eth_5m.csv", "predictions_eth.csv"):
        assert not (tmp_path / name).exists(), f"{name} 应被面板清除"


def test_api_ui_toggle(server):
    base, holder = server
    status, data = _post(base + "/api/ui", {"show": True})
    assert status == 200
    assert data["ok"] is True and data["show_tui"] is True
    assert holder.show_tui is True
    status, data = _post(base + "/api/ui", {"show": False})
    assert holder.show_tui is False


def test_api_status_includes_show_tui(server):
    base, holder = server
    holder.show_tui = True
    status, body = _get(base + "/api/status")
    assert json.loads(body)["show_tui"] is True


def test_html_book_uses_price_cents():
    """盘口显示必须用 priceCents（与 TUI 一致的美分格式），不得用 toFixed(2)。"""
    from pmbot.web_ui import WEB_HTML

    assert "priceCents(d.prices.up_bid)" in WEB_HTML
    assert "priceCents(d.prices.down_bid)" in WEB_HTML


def test_html_book_format_matches_tui():
    """盘口文案与 TUI 对齐：UP: bid/ask  DOWN: bid/ask（买/卖），美分整数。"""
    from pmbot.web_ui import WEB_HTML

    assert "UP: \" +" in WEB_HTML and "DOWN: \" +" in WEB_HTML
    assert "（买/卖）" in WEB_HTML
    assert "priceCents(d.prices.up_bid)" in WEB_HTML


def test_api_mode_switch(monkeypatch):
    """/api/mode：切换成功；运行中 409；非法参数 400。"""
    calls = []
    holder = ProcessControl(show_tui=True, live=False, paths=paths_for(False))
    lock = threading.Lock()
    srv = start_server(lambda: {"symbol": "ETH"}, "config.yaml", False, port=0,
                       holder=holder, lock=lock,
                       token="test-token",
                       set_mode_fn=lambda live: calls.append(live))
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: False)
        st, data = _post(base + "/api/mode", {"live": True})
        assert st == 200 and data["ok"] is True and calls == [True]
        monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: True)
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base + "/api/mode", {"live": False})
        assert ei.value.code == 409
        monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: False)
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base + "/api/mode", {"live": "yes"})
        assert ei.value.code == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_reset_rejected_in_live(monkeypatch):
    """实盘模式（holder.live=True）拒绝清除数据。"""
    holder = ProcessControl(live=True, paths=paths_for(True))
    lock = threading.Lock()
    srv = start_server(lambda: {"symbol": "ETH"}, "config.yaml", False, port=0,
                       holder=holder, lock=lock, token="test-token")
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: False)
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base + "/api/control", {"cmd": "reset"})
        assert ei.value.code == 409
        assert "实盘" in ei.value.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_js_is_valid_javascript(tmp_path):
    """页面内联 JS 必须能通过语法解析。

    防回归：WEB_HTML 是运行时字符串，若 JS 字符串里误写 \n 等转义被
    Python 展开成真实换行，会把 JS 字符串拦腰截断（整个 script 失效，
    页面永远停留在“连接中…”）。用 node --check 对运行时内容做校验。
    Windows 上 node 从 stdin 检查存在句柄竞态（偶发挂起），改用临时文件。
    """
    import re
    import shutil
    import subprocess

    import pmbot.web_ui as web_ui

    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用，跳过 JS 语法检查")
    html = web_ui.WEB_HTML  # 运行时字符串（转义已展开）
    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    js_file = tmp_path / "check.js"
    js_file.write_text(js, encoding="utf-8")
    r = subprocess.run([node, "--check", str(js_file)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"页面 JS 语法错误:\n{r.stderr}"


def test_api_control_uses_mode_paths_after_switch(monkeypatch):
    """切换模式后 stop/resume/reset 必须走新模式的路径（原 bug：闭包写死初始 data/，
    实盘主循环读 data_live/control.json，UI 指令却写 data/control.json 石沉大海）。"""
    from pmbot.paths import ProcessControl, paths_for
    from pmbot.web_ui import start_server

    holder = ProcessControl(live=True, paths=paths_for(True))  # 已切到实盘
    calls = []
    lock = threading.Lock()
    monkeypatch.setattr("pmbot.paths.ProcessControl.loop_alive", lambda self: True)
    monkeypatch.setattr("pmbot.web_ui.write_control",
                        lambda cmd, path: calls.append((cmd, str(path))))
    srv = start_server(lambda: {"symbol": "ETH"}, "config.yaml", False, port=0,
                       holder=holder, lock=lock, token="test-token")
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        for cmd in ("stop", "resume"):
            status, data = _post(base + "/api/control", {"cmd": cmd})
            assert status == 200 and data["ok"] is True
        assert calls == [("stop", str(Path("data_live/control.json"))),
                         ("resume", str(Path("data_live/control.json")))], calls
    finally:
        srv.shutdown()
        srv.server_close()


def test_trade_reason_label_single_source_of_truth():
    """平仓原因文案单一事实源：JS 必须用 PanelView 的 label 字段，不得自带 REASONS 映射。

    回归：曾有两份漂移文案（Python EXIT_LABELS "已止盈" vs JS REASONS "止盈"），
    且 PanelView 已输出 label 但 JS 零引用——深 seam 空置导致跨语言重复。
    """
    from pmbot.web_ui import WEB_HTML

    assert "t.label" in WEB_HTML
    assert "const REASONS" not in WEB_HTML


def test_post_loopback_requires_no_token(server):
    """本机回环访问免 token：无 token / 错 token 均放行（单机使用零负担）。"""
    base, _ = server
    status, data = _post(base + "/api/control", {"cmd": "stop"}, token="")
    assert status == 200 and data["ok"] is True
    status, data = _post(base + "/api/control", {"cmd": "stop"}, token="wrong-token")
    assert status == 200 and data["ok"] is True
    # 只读不受限
    status, _ = _get(base + "/api/status")
    assert status == 200


def test_authorized_remote_requires_token():
    """局域网访问必须携带正确 token；未配置 token 时拒绝一切写操作（安全默认）。"""
    from pmbot.web_ui import _authorized

    # 回环（IPv4/IPv6/localhost）免 token
    assert _authorized("127.0.0.1", "", "t")
    assert _authorized("::1", "", "t")
    assert _authorized("localhost", "", "t")
    # 局域网：无 token / 错 token 拒绝，对 token 放行
    assert not _authorized("192.168.1.5", "", "t")
    assert not _authorized("192.168.1.5", "wrong", "t")
    assert _authorized("192.168.1.5", "t", "t")
    # 未配置 token：一律拒绝（安全默认）
    assert not _authorized("127.0.0.1", "", None)
    assert not _authorized("127.0.0.1", "t", None)
    assert not _authorized("192.168.1.5", "t", None)
