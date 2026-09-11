"""实盘前置自检：凭证 / 余额 / 授权 / 地理资格 的硬阻塞，与最小单量的非阻塞提醒。

不碰网络：地理资格用注入的 geo 参数（geoblock_status 的解析单独用替身测）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pmbot import preflight
from pmbot.preflight import geoblock_status, live_advisories, live_preflight, min_amount_for_rules

MAX_UINT = "115792089237316195423570985008687907853269984665640564039457584007913129639935"
OK_GEO = (False, "country=VN region=44 ip=1.2.3.4")


def make_cfg(amount: float = 1.0, max_entry: float = 0.50) -> SimpleNamespace:
    return SimpleNamespace(amount_per_trade=amount, max_entry_price=max_entry)


class FakeExec:
    """collateral_snapshot 替身：可控余额/授权/异常。"""

    def __init__(self, balance=10.0, allowances=None, exc: Exception | None = None) -> None:
        self._balance = balance
        self._allowances = {"0xexchange": MAX_UINT} if allowances is None else allowances
        self._exc = exc

    def collateral_snapshot(self):
        if self._exc is not None:
            raise self._exc
        return self._balance, self._allowances


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    """默认给出完整凭证；单个用例可自行覆盖。"""
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("PROXY_WALLET", "0x" + "2" * 40)


# ---- 硬阻塞项 ----


def test_正常配置通过自检():
    assert live_preflight(make_cfg(), FakeExec(), geo=OK_GEO) == []


def test_余额为零被拦下():
    problems = live_preflight(make_cfg(), FakeExec(balance=0.0), geo=OK_GEO)
    assert any("余额" in p and "充值" in p for p in problems)


def test_授权为零被拦下():
    problems = live_preflight(make_cfg(), FakeExec(allowances={"0xexchange": "0"}), geo=OK_GEO)
    assert any("allowance" in p for p in problems)


def test_授权表为空被拦下():
    problems = live_preflight(make_cfg(), FakeExec(allowances={}), geo=OK_GEO)
    assert any("allowance" in p for p in problems)


def test_余额少于每注被拦下():
    problems = live_preflight(make_cfg(amount=3.0), FakeExec(balance=2.0), geo=OK_GEO)
    assert any("少于每注" in p for p in problems)


def test_查询异常被拦下():
    problems = live_preflight(make_cfg(), FakeExec(exc=RuntimeError("代理不通")), geo=OK_GEO)
    assert any("查询失败" in p for p in problems)


def test_缺少凭证被拦下(monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    problems = live_preflight(make_cfg(), FakeExec(), geo=OK_GEO)
    assert any("PRIVATE_KEY" in p for p in problems)


def test_响应异常余额为None被拦下():
    problems = live_preflight(make_cfg(), FakeExec(balance=None), geo=OK_GEO)
    assert any("无法读取" in p for p in problems)


# ---- 地理资格：服务端按出口 IP 禁止下单 ----


def test_出口被封禁止启动():
    """2026-09-10 实测：本机节点出口 AWS 东京，连续 15 次 blocked=true。"""
    problems = live_preflight(make_cfg(), FakeExec(), geo=(True, "country=JP ip=13.208.188.96"))
    assert any("地理限制" in p and "JP" in p for p in problems)


def test_地理资格无法确认也拦下():
    problems = live_preflight(make_cfg(), FakeExec(), geo=(None, "Timeout: 代理不通"))
    assert any("无法确认" in p for p in problems)


def test_出口未受限不拦():
    assert live_preflight(make_cfg(), FakeExec(), geo=(False, "country=VN")) == []


# ---- geoblock_status 解析（HTTP 用替身） ----


def test_geoblock_status_解析(monkeypatch):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"blocked": True, "ip": "13.208.188.96", "country": "JP", "region": ""}

    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    blocked, detail = geoblock_status()
    assert blocked is True and "JP" in detail and "13.208.188.96" in detail


def test_geoblock_status_网络失败降级为None(monkeypatch):
    def boom(*a, **k):
        raise OSError("proxy down")

    monkeypatch.setattr("requests.get", boom)
    blocked, detail = geoblock_status()
    assert blocked is None and "proxy down" in detail


def test_geoblock_status_响应缺字段(monkeypatch):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"country": "VN"}

    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    blocked, _ = geoblock_status()
    assert blocked is None  # 字段缺失 → 无法确认，不当作通过


# ---- 最小单量：不阻塞，只提醒 ----


def test_低于5股不阻塞启动_只出提醒():
    """$1 在 0.50 上限约 2 股 < 5 股：但该下限只针对 GTC/GTD 限价单，
    市价单(FOK)实证可成交 1.2 股单（py-clob-client #301）→ 不得拦启动。"""
    assert live_preflight(make_cfg(amount=1.0, max_entry=0.50), FakeExec(), geo=OK_GEO) == []
    notes = live_advisories(make_cfg(amount=1.0, max_entry=0.50))
    assert notes and "5 股" in notes[0] and "市价单" in notes[0]


def test_高于5股无提醒():
    assert live_advisories(make_cfg(amount=3.0, max_entry=0.50)) == []


def test_关闭入场上限时用兜底上限推算提醒():
    assert live_advisories(make_cfg(amount=1.0, max_entry=0.0))


@pytest.mark.parametrize(
    "cap,expected", [(0.50, 2.5), (0.20, 1.0), (1.00, 5.0), (0.0, 2.5)]
)
def test_最小金额换算(cap, expected):
    assert min_amount_for_rules(cap) == pytest.approx(expected)
