"""验证 XGBoost + edge gating 在 5m 方向能否扣费正。

bot 方法:25 特征 XGBoost + Platt 校准 + edge ≥7% 才入场。
本脚本用 binance 5m K线(可算的 OHLCV 技术指标子集)+ walk-forward,测:
  - 全样本准确率(预期~50%,随机游走)
  - edge gating 后子集准确率(只在 |p-0.5|>=gate 时入场)
  - 模拟扣 3% 费后期望
"""
from __future__ import annotations
import json, urllib.request, time, math
import numpy as np

KLINE_URL = "https://data-api.binance.vision/api/v3/klines"
FEE = 0.03  # Polymarket taker 双边


def fetch_klines(symbol="BTCUSDT", interval="5m", limit=1000, end_ts=None):
    """拉 binance 5m K线。"""
    all_k = []
    end = end_ts or int(time.time() * 1000)
    while len(all_k) < limit:
        params = f"?symbol={symbol}&interval={interval}&endTime={end}&limit=1000"
        url = KLINE_URL + params
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        data = json.load(urllib.request.urlopen(req, timeout=30))
        if not data: break
        all_k = data + all_k
        end = data[0][0] - 1
        if len(data) < 1000: break
        time.sleep(0.2)
    return all_k[:limit]


def to_features(klines):
    """OHLCV -> 技术指标特征 + 标签(next candle up/down)。"""
    closes = np.array([float(k[4]) for k in klines])
    highs = np.array([float(k[2]) for k in klines])
    lows = np.array([float(k[3]) for k in klines])
    vols = np.array([float(k[5]) for k in klines])
    n = len(closes)
    feats = []
    for i in range(15, n - 1):  # 留前 15 算指标,最后一根做标签
        c = closes[:i+1]
        def ret(k):
            j = max(0, i - k)
            return (closes[i] - closes[j]) / closes[j] if closes[j] else 0
        # RSI(14)
        deltas = np.diff(c[-15:])
        ups = np.mean([d for d in deltas if d > 0] or [0])
        dns = np.mean([-d for d in deltas if d < 0] or [0])
        rsi = 100 - 100 / (1 + ups / dns) if dns else 100
        # MACD(12,26,9) 简化
        ema12 = c[-12:].mean() if len(c) >= 12 else c.mean()
        ema26 = c[-26:].mean() if len(c) >= 26 else c.mean()
        macd = ema12 - ema26
        # BB %B
        sma20 = c[-20:].mean() if len(c) >= 20 else c.mean()
        std20 = c[-20:].std() if len(c) >= 20 else c.std()
        pctb = (closes[i] - (sma20 - 2*std20)) / (4*std20) if std20 else 0.5
        # vol regime
        vol_reg = 1 if vols[max(0,i-20):i].mean() < vols[i] else 0
        feats.append({
            "rsi": rsi, "macd": macd, "pctb": pctb,
            "ret1": ret(1), "ret3": ret(3), "ret5": ret(5), "ret15": ret(15),
            "vol_regime": vol_reg, "range": (highs[i]-lows[i])/closes[i],
            "momentum": ret(10),
        })
    feats = np.array([list(f.values()) for f in feats], dtype=np.float32)
    labels = (closes[16:] > closes[15:-1]).astype(np.int32)  # next up
    return feats, labels


def main():
    print("拉 binance 5m K线(3000根)...")
    klines = fetch_klines(limit=3000)
    print(f"拿到 {len(klines)} 根")
    X, y = to_features(klines)
    print(f"特征 {X.shape} 标签 {y.shape} up率 {y.mean():.1%}")

    try:
        import xgboost as xgb
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.metrics import roc_auc_score
    except ImportError:
        print("需要 xgboost scikit-learn: uv pip install xgboost scikit-learn")
        return

    # walk-forward: 前 70% 训练,后 30% 测试
    split = int(len(X) * 0.70)
    Xtr, Xte = X[:split], X[split:]
    ytr, yte = y[:split], y[split:]
    base = xgb.XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.05,
                             subsample=0.8, colsample_bytree=0.8,
                             eval_metric="logloss", random_state=42, n_jobs=-1)
    clf = CalibratedClassifierCV(base, method="sigmoid", cv=3)
    clf.fit(Xtr, ytr)
    p = clf.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(yte, p)
    acc = ((p > 0.5) == yte).mean()
    print(f"\n全样本(测试集): AUC={auc:.4f} 准确率={acc:.1%}")

    # edge gating: 只在 |p-0.5| >= gate 入场
    print(f"\n=== edge gating 后(模拟 Polymarket 3% 双边费) ===")
    print(f"{'gate':>5} {'交易数':>6} {'命中率':>7} {'净期望/笔':>10}")
    for gate in [0.00, 0.05, 0.07, 0.10, 0.15, 0.20, 0.30]:
        mask = np.abs(p - 0.5) >= gate
        if mask.sum() < 20:
            print(f"{gate:>5.2f} {mask.sum():>6} (样本不足)")
            continue
        preds = (p > 0.5).astype(int)
        hits = (preds[mask] == yte[mask]).mean()
        # Polymarket 入场价 = 市场隐含(这里用 p 近似,实际应 vs Polymarket 价)
        # 简化:入场 0.5(假设盘口 0.5),扣费 3%,赢 +0.47 输 -0.53... 用真实价更准
        entry = 0.5
        net = hits * (1 - entry) * (1 - FEE) - (1 - hits) * entry * (1 + FEE)
        print(f"{gate:>5.2f} {mask.sum():>6} {hits:>6.1%} {net:>+9.4f}")


if __name__ == "__main__":
    main()