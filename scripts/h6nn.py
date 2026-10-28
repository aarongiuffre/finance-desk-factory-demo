#!/usr/bin/env python3
"""h6nn.py - H6_nn_quantile signal: MLP tercile classifier on BTC 15m (book 18.2).

Look-ahead contract (prereg H6_nn_quantile.yaml): features at bar t read only
bars <= t; tercile cutoffs and feature standardization come from the current
training window only; a day's model is fit at the UTC day boundary on bars with
timestamp < the boundary and complete label windows.  Truncating the tape at D
leaves every prediction and position through D unchanged (scripts/h6_test.py
proves this and kills a future-peeking mutant).
"""
import numpy as np

HORIZONS = [12, 48, 96]
SEED = 20261028


def rsi(closes, n=14):
    """Simple mean-gain/loss RSI in [0, 100]; NaN during warmup."""
    c = np.asarray(closes, dtype=float)
    d = np.diff(c, prepend=np.nan)
    out = np.full(len(c), np.nan)
    for t in range(n, len(c)):
        seg = d[t - n + 1: t + 1]
        if np.isnan(seg).any():
            continue
        up = seg[seg > 0].mean() if (seg > 0).any() else 0.0
        dn = -seg[seg < 0].mean() if (seg < 0).any() else 0.0
        out[t] = 100.0 if dn == 0 else 100.0 - 100.0 / (1.0 + up / dn)
    return out


def build_features(closes):
    """m=4 causal features: z-scored last-bar return at 3 horizons + RSI/100."""
    closes = np.asarray(closes, dtype=float)
    r = np.full(len(closes), np.nan)
    r[1:] = closes[1:] / closes[:-1] - 1.0
    cols = []
    for H in HORIZONS:
        f = np.full(len(closes), np.nan)
        for t in range(H, len(closes)):
            seg = r[t - H + 1: t + 1]
            if not np.isnan(seg).any():
                mu, sd = seg.mean(), seg.std(ddof=0)
                f[t] = (r[t] - mu) / sd if sd > 0 else 0.0
        cols.append(f)
    cols.append(rsi(closes) / 100.0)
    return np.column_stack(cols)


def forward_classes(closes, f_bars, lo, hi):
    """Tercile cutoffs of the forward return over bars [lo, hi) (training only)."""
    closes = np.asarray(closes, dtype=float)
    fwd = np.full(hi - lo, np.nan)
    for t in range(lo, hi):
        if t + f_bars < len(closes):
            fwd[t - lo] = closes[t + f_bars] / closes[t] - 1.0
    fwd = fwd[~np.isnan(fwd)]
    return np.quantile(fwd, [1 / 3, 2 / 3])


def classify(fwd_ret, cutoffs):
    return np.where(fwd_ret <= cutoffs[0], 0, np.where(fwd_ret < cutoffs[1], 1, 2)).astype(int)


class MLP:
    """4 -> 16 ReLU -> 3 softmax, cross-entropy, Adam.  Deterministic given the seed."""

    def __init__(self, m, seed=SEED):
        rng = np.random.default_rng(seed)
        self.W1 = rng.normal(0, 0.3, (m, 16))
        self.b1 = np.zeros(16)
        self.W2 = rng.normal(0, 0.3, (16, 3))
        self.b2 = np.zeros(3)
        self.m = {k: np.zeros_like(v) for k, v in
                  [("W1", self.W1), ("b1", self.b1), ("W2", self.W2), ("b2", self.b2)]}
        self.v = {k: np.zeros_like(v) for k, v in
                  [("W1", self.W1), ("b1", self.b1), ("W2", self.W2), ("b2", self.b2)]}
        self.step = 0

    def forward(self, X):
        h = np.maximum(X @ self.W1 + self.b1, 0.0)
        z = h @ self.W2 + self.b2
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        p = e / e.sum(axis=1, keepdims=True)
        return h, p

    def train(self, X, y, epochs=30, lr=0.01, batch=256):
        n = len(X)
        for _ in range(epochs):
            order = np.random.default_rng(SEED + self.step).permutation(n)
            for s in range(0, n, batch):
                idx = order[s: s + batch]
                Xb, yb = X[idx], y[idx]
                h, p = self.forward(Xb)
                dz = p.copy()
                dz[np.arange(len(idx)), yb] -= 1.0
                gW2 = h.T @ dz / len(idx)
                gb2 = dz.mean(axis=0)
                dh = dz @ self.W2.T * (h > 0)
                gW1 = Xb.T @ dh / len(idx)
                gb1 = dh.mean(axis=0)
                self.step += 1
                for k, g in [("W1", gW1), ("b1", gb1), ("W2", gW2), ("b2", gb2)]:
                    self.m[k] = 0.9 * self.m[k] + 0.1 * g
                    self.v[k] = 0.999 * self.v[k] + 0.001 * g ** 2
                    mh = self.m[k] / (1 - 0.9 ** self.step)
                    vh = self.v[k] / (1 - 0.999 ** self.step)
                    upd = lr * mh / (np.sqrt(vh) + 1e-8)
                    if k == "W1":
                        self.W1 -= upd
                    elif k == "b1":
                        self.b1 -= upd
                    elif k == "W2":
                        self.W2 -= upd
                    else:
                        self.b2 -= upd

    def predict(self, X):
        _, p = self.forward(X)
        return p.argmax(axis=1)


def positions(closes, ts, f_bars, train_days, val_start, val_end, flat=False,
              epochs=30):
    """Daily-retrained positions over [val_start, val_end].

    At each UTC day boundary the model is fit on the trailing train_days days of
    bars with timestamp < boundary and complete label windows; cutoffs and
    standardization come from that window only.  ts is a datetime64 array.
    Returns (pos, acc): acc is out-of-sample 3-class accuracy over val bars
    (post-hoc, per-day cutoffs).
    """
    closes = np.asarray(closes, dtype=float)
    ts = np.asarray(ts)
    feat = build_features(closes)
    n = len(closes)
    dates = ts.astype("datetime64[D]")
    day_starts = [i for i in range(1, n) if dates[i] != dates[i - 1]]
    pos = np.full(n, np.nan)
    pred_cls = np.full(n, -1)
    true_cls = np.full(n, -1)
    cur = 0.0
    for d0 in day_starts:
        if d0 < val_start:
            continue
        d1 = min(n, val_end + 1)
        nxt = [i for i in day_starts if i > d0]
        d1 = min(d1, nxt[0] if nxt else n)
        tr_hi = d0 - f_bars            # labels complete before the boundary
        tr_lo = int(np.searchsorted(ts, ts[d0] - np.timedelta64(train_days, "D")))
        if tr_hi - tr_lo < 500:
            continue
        Xtr_raw = feat[tr_lo: tr_hi]
        ytr_fwd = np.array([closes[t + f_bars] / closes[t] - 1.0 for t in range(tr_lo, tr_hi)])
        ok = ~np.isnan(ytr_fwd) & ~np.isnan(Xtr_raw).any(axis=1)
        cutoffs = forward_classes(closes, f_bars, tr_lo, tr_hi)
        mu, sd = Xtr_raw[ok].mean(axis=0), Xtr_raw[ok].std(axis=0)
        sd = np.where(sd == 0, 1.0, sd)
        model = MLP(feat.shape[1])
        model.train((Xtr_raw[ok] - mu) / sd, classify(ytr_fwd[ok], cutoffs), epochs=epochs)
        Xd = feat[d0: d1]
        pred = model.predict((Xd - mu) / sd)
        pred_cls[d0: d1] = pred
        fwd = np.array([closes[t + f_bars] / closes[t] - 1.0 if t + f_bars < n else np.nan
                        for t in range(d0, d1)])
        for j in range(d0, d1):
            if not np.isnan(fwd[j - d0]) and not np.isnan(Xd[j - d0]).any():
                true_cls[j] = classify(np.array([fwd[j - d0]]), cutoffs)[0]
        for j, t in enumerate(range(d0, d1)):
            if np.isnan(Xd[j]).any():
                pos[t] = cur if cur != 0 else np.nan
                continue
            target = 0.0
            if pred[j] == 2:
                target = 1.0
            elif (not flat) and pred[j] == 0:
                target = -1.0
            if target != cur:
                cur = target
            pos[t] = cur
    okm = true_cls >= 0
    acc = float((pred_cls[okm] == true_cls[okm]).mean()) if okm.any() else np.nan
    return pos, acc
