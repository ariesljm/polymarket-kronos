"""一键启动器测试：主循环子进程 + 面板编排。"""

import subprocess

import pytest


def test_start_bot_spawns_run_and_stops(monkeypatch):
    """start_bot 启动主循环子进程；面板退出（KeyboardInterrupt）后停子进程。"""
    from pmbot import start_bot

    proc = {"terminated": False}

    class FakeProc:
        pid = 12345

        def terminate(self):
            proc["terminated"] = True

        def wait(self, timeout=None):
            return 0

    calls = []
    monitor_calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: (calls.append((a, kw)), FakeProc())[1])

    # 单实例守护隔离：不检测/不杀真实运行中的 bot（避免测试碰真实 bot.pids 与进程）
    class FakeGuard:
        def __init__(self, *a, **kw):
            pass

        def kill_old(self, role):
            pass

        def register(self, role):
            pass

        def unregister(self, role):
            pass

    monkeypatch.setattr("pmbot.single_instance.InstanceGuard", FakeGuard)

    def fake_monitor(argv=None):
        monitor_calls.append(argv)
        raise KeyboardInterrupt

    monkeypatch.setattr("pmbot.monitor.main", fake_monitor)
    monkeypatch.setattr("pmbot.run.main", lambda *a, **kw: None)
    killed = []
    monkeypatch.setattr(start_bot, "_kill_tree", lambda pid: killed.append(pid))  # 整树杀（避免真实 taskkill）

    assert start_bot.main(["--dry-run"]) == 0
    assert calls, "应启动主循环子进程"
    assert killed == [12345], "面板退出后应整树终止主循环（uv shim + base python）"
    # 面板以 web-only 模式启动（不渲染终端面板，浏览器打开 Web 控制台）
    assert monitor_calls == [["--dry-run", "--config", "config.yaml", "--web-only", "--data-dir", "data"]]


def test_run_with_guard_order_and_cleanup(monkeypatch):
    """run_with_guard: 杀旧→注册→执行→注销（含异常路径）。"""
    import pmbot.single_instance as si

    calls = []

    class FakeGuard:
        def __init__(self, *a, **kw):
            pass

        def kill_old(self, role):
            calls.append(f"kill:{role}")

        def register(self, role):
            calls.append(f"reg:{role}")

        def unregister(self, role):
            calls.append(f"unreg:{role}")

    monkeypatch.setattr(si, "InstanceGuard", FakeGuard)
    result = si.run_with_guard("run", lambda: 42)
    assert result == 42
    assert calls == ["kill:run", "reg:run", "unreg:run"]

    # 异常路径也注销
    calls.clear()
    try:
        si.run_with_guard("run", lambda: 1 / 0)
    except ZeroDivisionError:
        pass
    assert calls == ["kill:run", "reg:run", "unreg:run"]
