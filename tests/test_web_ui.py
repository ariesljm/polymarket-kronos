"""Web 控制台测试：HTTP API（起真实 ThreadingHTTPServer，随机端口）。"""

import json
import threading
import urllib.request

import pytest

from pmbot.paths import ProcessControl, paths_for
from pmbot.web_ui import start_server


@pytest.fixture
def server():
    holder = ProcessControl()
    lock = threading.Lock()

    def view_fn():
        return {"symbol": "ETH", "paused": False, "position": None,
                "recent_trades": [{"ts": "10:00", "direction": "up", "pnl": 1.0}]}

    srv = start_server(view_fn, "config.yaml", False, port=0, holder=holder, lock=lock)
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}", holder
    srv.shutdown()
    srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def _post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
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
    monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: True)
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
    monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: True)  # 运行中 → 走指令通道
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
    monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: False)

    class FakeProc:
        pid = 9999

        def poll(self):
            return None

    calls = []
    monkeypatch.setattr("pmbot.web_ui.spawn_loop",
                        lambda config, live, data_dir="data": (calls.append((config, live)), FakeProc())[1])
    status, data = _post(base + "/api/control", {"cmd": "start"})
    assert status == 200
    assert data["started"] is True
    assert calls == [("config.yaml", False)]
    assert holder.proc is not None


def test_api_control_start_conflict(server, monkeypatch):
    base, holder = server
    monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: True)
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(base + "/api/control", {"cmd": "start"})
    assert ei.value.code == 409


def test_api_reset_when_loop_stopped_clears_files(tmp_path, monkeypatch):
    """主循环停止时 reset 由面板直接清除数据文件（解除死锁）。"""
    from pmbot.paths import ProcessControl, paths_for
    from pmbot.web_ui import start_server

    (tmp_path / "status.json").write_text('{"symbol": "ETH"}', encoding="utf-8")
    (tmp_path / "trades.csv").write_text("ts\nx", encoding="utf-8")
    (tmp_path / "eth_5m.csv").write_text("t", encoding="utf-8")
    (tmp_path / "predictions_eth.csv").write_text("t", encoding="utf-8")
    monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: False)

    srv = start_server(lambda: {"symbol": "ETH"}, "config.yaml", False, port=0,
                       holder=ProcessControl(), lock=threading.Lock(),
                       status_path=str(tmp_path / "status.json"),
                       trades_path=str(tmp_path / "trades.csv"),
                       log_dir=str(tmp_path))
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
                       set_mode_fn=lambda live: calls.append(live))
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: False)
        st, data = _post(base + "/api/mode", {"live": True})
        assert st == 200 and data["ok"] is True and calls == [True]
        monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: True)
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base + "/api/mode", {"live": False})
        assert ei.value.code == 409
        monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: False)
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
                       holder=holder, lock=lock)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        monkeypatch.setattr("pmbot.web_ui.loop_alive", lambda *a: False)
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(base + "/api/control", {"cmd": "reset"})
        assert ei.value.code == 409
        assert "实盘" in ei.value.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_js_is_valid_javascript():
    """页面内联 JS 必须能通过语法解析。

    防回归：WEB_HTML 是运行时字符串，若 JS 字符串里误写 \n 等转义被
    Python 展开成真实换行，会把 JS 字符串拦腰截断（整个 script 失效，
    页面永远停留在“连接中…”）。用 node --check 对运行时内容做校验。
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
    r = subprocess.run([node, "--check", "-"], input=js,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"页面 JS 语法错误:\n{r.stderr}"
