"""KronosPredictorClient 设备选择测试：GPU 检测 / CPU 回退。"""

import pytest

from pmbot.predictor import KronosPredictorClient


@pytest.fixture
def fake_vendor(monkeypatch):
    """替换 vendor_kronos 为 fake（不真加载模型/权重）。"""
    captured = {}

    class FakeKronos:
        @staticmethod
        def from_pretrained(path):
            return object()

    class FakeTokenizer:
        @staticmethod
        def from_pretrained(path):
            return object()

    class FakeKronosPredictor:
        def __init__(self, model, tokenizer, device=None, max_context=512, clip=5):
            captured["device"] = device
            captured["max_context"] = max_context

    monkeypatch.setattr("pmbot.vendor_kronos.Kronos", FakeKronos)
    monkeypatch.setattr("pmbot.vendor_kronos.KronosTokenizer", FakeTokenizer)
    monkeypatch.setattr("pmbot.vendor_kronos.KronosPredictor", FakeKronosPredictor)
    return captured


def test_device_auto_cuda_when_available(fake_vendor, monkeypatch):
    """主机有 GPU（torch.cuda.is_available=True）→ 推理设备 cuda。"""
    monkeypatch.setattr("torch.cuda.is_available", lambda: True)
    client = KronosPredictorClient(variant="kronos-small")
    client._load()
    assert fake_vendor["device"] == "cuda"


def test_device_auto_cpu_when_no_gpu(fake_vendor, monkeypatch):
    """无 GPU → 回退 cpu。"""
    monkeypatch.setattr("torch.cuda.is_available", lambda: False)
    client = KronosPredictorClient(variant="kronos-small")
    client._load()
    assert fake_vendor["device"] == "cpu"


def test_device_explicit_override(fake_vendor, monkeypatch):
    """显式指定 device 时不检测，直接使用。"""
    monkeypatch.setattr("torch.cuda.is_available", lambda: True)
    client = KronosPredictorClient(variant="kronos-small", device="cpu")
    client._load()
    assert fake_vendor["device"] == "cpu"


def test_predict_targets_batches_samples(monkeypatch, tmp_path):
    """sample_count=100 → 一次批量调用（非串行 100 次）。"""
    from pmbot.predictor import KronosPredictorClient

    calls = {"n": 0, "sample_count": None, "results": []}

    class FakeVendorPredictor:
        def __init__(self, model, tokenizer, device=None, max_context=512, clip=5):
            pass

        def predict(self, df, x_timestamp, y_timestamp, pred_len, T=1.0, top_k=0,
                    top_p=0.9, sample_count=1, verbose=True, return_all=False):
            calls["n"] += 1
            calls["sample_count"] = sample_count
            calls["return_all"] = return_all
            import pandas as pd
            return pd.DataFrame({"close": [float(i) for i in range(sample_count)]})

    monkeypatch.setattr("pmbot.vendor_kronos.Kronos", type("K", (), {
        "from_pretrained": staticmethod(lambda p: object())}))
    monkeypatch.setattr("pmbot.vendor_kronos.KronosTokenizer", type("T", (), {
        "from_pretrained": staticmethod(lambda p: object())}))
    monkeypatch.setattr("pmbot.vendor_kronos.KronosPredictor", FakeVendorPredictor)

    import pandas as pd
    df = pd.DataFrame({
        "timestamp": [1_700_000_000_000 + i * 300_000 for i in range(30)],
        "open": [1.0] * 30, "high": [1.1] * 30, "low": [0.9] * 30,
        "close": [1.0] * 30, "volume": [10.0] * 30,
    })
    client = KronosPredictorClient(variant="kronos-small")
    closes = client.predict_targets(df, sample_count=100)
    assert calls["n"] == 1, "应一次批量调用"
    assert calls["sample_count"] == 100
    assert calls["return_all"] is True, "应保留全部独立样本（不均值）"
    assert len(closes) == 100
