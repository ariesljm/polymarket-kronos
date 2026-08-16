"""控制通道测试：control.json 指令写入/消费。"""

import json

import pytest

from pmbot import control
from pmbot.control import read_control, write_control


def test_write_and_read_roundtrip(tmp_path):
    p = tmp_path / "control.json"
    for cmd in ("resume", "reset", "stop"):
        write_control(cmd, p)
        assert read_control(p) == cmd
        assert not p.exists(), "指令消费后应删除文件"


def test_read_no_file(tmp_path):
    assert read_control(tmp_path / "nope.json") is None


def test_write_invalid_cmd(tmp_path):
    with pytest.raises(ValueError):
        write_control("rm -rf", tmp_path / "control.json")


def test_read_invalid_cmd_removes_file(tmp_path):
    p = tmp_path / "control.json"
    p.write_text(json.dumps({"cmd": "hack"}), encoding="utf-8")
    assert read_control(p) is None
    assert not p.exists(), "非法指令文件也应删除（防阻塞通道）"


def test_read_corrupt_json_removes_file(tmp_path):
    p = tmp_path / "control.json"
    p.write_text("{broken", encoding="utf-8")
    assert read_control(p) is None
    assert not p.exists()


def test_default_path_constant(tmp_path, monkeypatch):
    """默认路径 data/control.json；monkeypatch 可换 tmp 路径。"""
    monkeypatch.setattr(control, "CONTROL_FILE", str(tmp_path / "control.json"))
    write_control("resume")
    assert read_control() == "resume"


def test_clear_data_files_removes_runtime_files(tmp_path):
    """清除 status/trades/K线/预测文件，保留无关文件。"""
    from pmbot.control import clear_data_files

    (tmp_path / "status.json").write_text("{}", encoding="utf-8")
    (tmp_path / "trades.csv").write_text("ts", encoding="utf-8")
    (tmp_path / "eth_5m.csv").write_text("t", encoding="utf-8")
    (tmp_path / "predictions_eth.csv").write_text("t", encoding="utf-8")
    (tmp_path / "other.txt").write_text("keep", encoding="utf-8")

    clear_data_files(tmp_path / "status.json", tmp_path / "trades.csv",
                     tmp_path, "ETH", "5m")

    for name in ("status.json", "trades.csv", "eth_5m.csv", "predictions_eth.csv"):
        assert not (tmp_path / name).exists(), f"{name} 应被删除"
    assert (tmp_path / "other.txt").exists(), "无关文件应保留"


def test_clear_data_files_missing_symbol_skips_patterns(tmp_path):
    """symbol 为空时不做文件名匹配（不误删其他标的文件）。"""
    from pmbot.control import clear_data_files

    (tmp_path / "btc_5m.csv").write_text("t", encoding="utf-8")
    clear_data_files(tmp_path / "nope.json", tmp_path / "nope.csv", tmp_path, "", "5m")
    assert (tmp_path / "btc_5m.csv").exists()
