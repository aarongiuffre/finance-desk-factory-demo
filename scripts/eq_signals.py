#!/usr/bin/env python3
"""eq_signals.py - signal/weight builders for the Eq_* families.

Every builder returns (W, meta): W = DataFrame on the panel calendar, row t =
weights ACTIVE during day t (decided at close t-1); builders read ONLY rows
<= t-1 of the panel (row-wise causality; replay-checked by the runner).
"""
import numpy as np
import pandas as pd

from eq_lib import members_at

TUNE_LO_G, TUNE_HI_G = "2015-01-01", "2017-12-31"

MONTH_ENDS_CACHE = {}


def month_ends(cal):
    key = (cal[0], cal[-1])
    if key not in MONTH_ENDS_CACHE:
        s = pd.Series(cal, index=cal)
        MONTH_ENDS_CACHE[key] = s.groupby(s.index.to_period("M")).last()
    return MONTH_ENDS_CACHE[key]


def monthly_adj(panel):
    """Month-end adjclose per ticker (last non-NaN bar of each month)."""
    return panel["a"].resample("ME").last()


def monthly_returns(panel):
    p = monthly_adj(panel)
    return p.pct_change()


def month_index_of(cal, reb_dates):
    """map rebalance date -> position in the monthly frame."""
    return reb_dates


def eligibility(panel, cal, min_price=5.0, min_adv=1e6, min_bars=0):
    """member(D) & close>=min_price & ADV20>=min_adv & >=min_bars history."""
    ms = panel["membership"]
    tickers = panel["tickers"]
    mem = pd.DataFrame(False, index=cal, columns=tickers)
    for d in cal:
        cols = [t for t in members_at(ms, str(d.date())) if t in mem.columns]
        if cols:
            mem.loc[d, cols] = True
    px_ok = panel["c"] >= min_price
    adv_ok = panel["adv20"] >= min_adv
    bars = panel["a"].notna().cumsum()
    e = mem & px_ok.fillna(False) & adv_ok.fillna(False)
    if min_bars:
        e &= bars >= min_bars
    return e


def apply_rebalance(W, cal, reb_dates, weights_by_date):
    """Write fixed weights from the day after each rebalance to the next."""
    next_days = []
    for d in reb_dates:
        loc = cal.searchsorted(pd.Timestamp(d), side="right")
        next_days.append(loc)
    for k, d in enumerate(reb_dates):
        start = next_days[k]
        end = next_days[k + 1] if k + 1 < len(next_days) else len(cal)
        if start >= len(cal):
            continue
        w = weights_by_date.get(str(pd.Timestamp(d).date()))
        if w is None:
            continue
        W.iloc[start:end, :] = W.iloc[start:end, :].values * 0.0
        for t, v in w.items():
            if v != 0.0 and t in W.columns:
                W.iloc[start:end, W.columns.get_loc(t)] = v
    return W


def dn_weights(signals, n_leg_frac):
    """signals: Series (ticker -> value) with NaN for ineligible.
    Longs = top n_leg_frac by value desc, shorts = bottom n_leg_frac."""
    s = signals.dropna()
    n = len(s)
    if n < 10:
        return None, 0
    n_leg = max(1, int(round(n_leg_frac * n)))
    s = s.sort_values(ascending=False, kind="mergesort")
    longs = s.index[:n_leg]
    shorts = s.index[-n_leg:]
    w = {}
    for t in longs:
        w[t] = 1.0 / (2.0 * n_leg)
    for t in shorts:
        w[t] = -1.0 / (2.0 * n_leg)
    return w, n_leg


def _elig_bars(panel, elig, min_bars=0):
    if not min_bars:
        return elig
    bars = panel["a"].notna().cumsum()
    return elig & (bars >= min_bars)


# ------------------------------------------------------------- Eq_price_momentum
def build_momentum(panel, cal, elig, crit="rcum", leg_frac=0.1, hold_months=1):
    M = monthly_returns(panel)
    reb_dates = list(month_ends(cal))
    reb_dates = [d for d in reb_dates
                 if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    if hold_months > 1:
        reb_dates = reb_dates[::hold_months]
    wdates, pool_sizes = {}, []
    mi = {str(pd.Timestamp(d).date()): i for i, d in enumerate(M.index)}
    for D in reb_dates:
        e = elig.loc[D]
        if e.sum() < 10:
            continue
        key = str(pd.Timestamp(D).date())
        if key not in mi:
            continue
        m = mi[key]
        if m < 13:
            continue
        form = M.iloc[m - 12: m - 1]          # months m-12..m-2 (skip month m)
        ok = form.notna().all()
        e2 = e & ok
        if e2.sum() < 10:
            continue
        if crit == "rcum":
            sig = (form.loc[:, e2] + 1.0).prod() - 1.0
        else:
            sig = form.loc[:, e2].mean() / form.loc[:, e2].std(ddof=1)
        w, n_leg = dn_weights(sig, leg_frac)
        if w:
            wdates[key] = w
            pool_sizes.append(int(e2.sum()))
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb_dates, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb_dates],
               "n_episodes": len(wdates), "pool_sizes": pool_sizes}


# ------------------------------------------------------------- Eq_low_vol
def build_lowvol(panel, cal, elig, window=126, leg_frac=0.1, min_bars=0):
    elig = _elig_bars(panel, elig, min_bars)
    vol = panel["r"].rolling(window, min_periods=int(0.8 * window)).std(ddof=1)
    reb_dates = [d for d in month_ends(cal)
                 if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    wdates, pool_sizes = {}, []
    for D in reb_dates:
        e = elig.loc[D]
        sig = vol.loc[D]
        sig = sig[e & sig.notna()]
        w, n_leg = dn_weights(-sig, leg_frac)   # low vol = long bottom sigma
        if w:
            wdates[str(pd.Timestamp(D).date())] = w
            pool_sizes.append(int(len(sig)))
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb_dates, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb_dates],
               "n_episodes": len(wdates), "pool_sizes": pool_sizes}


# ------------------------------------------------------------- Eq_residual_momentum
def build_resmom(panel, cal, elig, reg_window=36, leg_frac=0.1):
    M = monthly_returns(panel)
    rf, f3 = ff_monthly(panel)
    reb_dates = [d for d in month_ends(cal)
                 if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    wdates, pool_sizes = {}, []
    mi = {str(pd.Timestamp(d).date()): i for i, d in enumerate(M.index)}
    for D in reb_dates:
        e = elig.loc[D]
        key = str(pd.Timestamp(D).date())
        if key not in mi:
            continue
        m = mi[key]
        if m < reg_window + 12:
            continue
        # regression window: months m-reg_window .. m-1
        Y = (M.iloc[m - reg_window: m] - rf.iloc[m - reg_window: m]).values   # (W, N)
        X = f3.iloc[m - reg_window: m].values                                  # (W, 3)
        ok_names = e.values & ~np.isnan(Y).any(axis=0)
        if ok_names.sum() < 10:
            continue
        Xc = X - X.mean(axis=0)
        Yc = Y[:, ok_names] - Y[:, ok_names].mean(axis=0)
        betas = np.linalg.lstsq(Xc, Yc, rcond=None)[0]                        # (3, K)
        resid_all = Y[:, ok_names] - X @ betas                                # (W, K)
        form = resid_all[-12:-1]                                              # months m-12..m-2
        mu = form.mean(axis=0)
        sd = form.std(axis=0, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            sig = mu / sd
        sig_s = pd.Series(sig, index=M.columns[ok_names])
        w, n_leg = dn_weights(sig_s, leg_frac)
        if w:
            wdates[key] = w
            pool_sizes.append(int(ok_names.sum()))
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb_dates, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb_dates],
               "n_episodes": len(wdates), "pool_sizes": pool_sizes}


def ff_monthly(panel):
    """Monthly FF3 (MKT, SMB, HML) + RF from the desk's Kenneth French daily zip."""
    import zipfile, io
    z = zipfile.ZipFile("/data/finance/raw/ff/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip")
    name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
    lines = z.read(name).decode("latin-1").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(",Mkt-RF"))
    rows = []
    for l in lines[start + 1:]:
        parts = [p.strip() for p in l.split(",")]
        if len(parts) >= 6 and parts[0].isdigit():
            rows.append(parts[:6])
        else:
            break
    df = pd.DataFrame(rows, columns=["date", "MKT", "SMB", "HML", "RMW", "CMA"])
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.set_index("date").astype(float) / 100.0
    m = df[["MKT", "SMB", "HML"]].resample("ME").apply(lambda x: (1 + x).prod() - 1)
    rf = df["RF"].resample("ME").last()
    # align to the panel's monthly grid
    mret = monthly_returns(panel)
    m = m.reindex(mret.index).ffill()
    rf = rf.reindex(mret.index).ffill()
    return rf, m


# ------------------------------------------------------------- Eq_pairs
def build_pairs(panel, cal, elig, corr_win=252, z_win=60, entry=2.0, n_pairs=50,
                min_corr=0.70, per_pair=0.5, min_bars=0):
    elig = _elig_bars(panel, elig, min_bars)
    reb_dates = [d for d in month_ends(cal)
                 if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    all_reb = list(month_ends(cal))
    la = np.log(panel["a"])
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    episode_count = 0
    pool_min = None
    for k, D in enumerate(reb_dates):
        e = elig.loc[D]
        names = e[e].index
        if len(names) < 20:
            continue
        R = panel["r"].loc[:D].tail(corr_win)
        R = R[names]
        C = R.corr(min_periods=int(0.8 * corr_win))
        C = C.where(np.triu(np.ones(C.shape), k=1).astype(bool))
        pairs = []
        used = set()
        flat = C.stack().sort_values(ascending=False)
        for (a, b), v in flat.items():
            if len(pairs) >= n_pairs:
                break
            if v < min_corr or a in used or b in used or pd.isna(v):
                continue
            pairs.append((a, b))
            used.add(a)
            used.add(b)
        if pool_min is None or len(names) < pool_min:
            pool_min = len(names)
        nxt = all_reb[all_reb.index(D) + 1] if k + 1 < len(reb_dates) else cal[-1]
        # day loop: decision at t-1 -> W at t
        pos = {}   # pair -> {'dir': +1/-1, 'w': dict}
        span = cal[(cal > D) & (cal <= pd.Timestamp(nxt))]
        for t in span:
            i = cal.get_loc(t)
            dec = cal[i - 1]
            for (a, b) in pairs:
                s = (la[a] - la[b]).loc[:dec].tail(z_win)
                s = s.dropna()
                if len(s) < int(0.8 * z_win):
                    continue
                sd = s.std(ddof=1)
                if not sd or not np.isfinite(sd) or sd == 0:
                    continue
                z = (s.iloc[-1] - s.mean()) / sd
                st = pos.get((a, b))
                if st is None:
                    if abs(z) >= entry:
                        d = 1.0 if z > 0 else -1.0   # z>0: A rich -> short A
                        pos[(a, b)] = {"dir": d}
                        W.loc[t, a] = -d * per_pair / n_pairs
                        W.loc[t, b] = d * per_pair / n_pairs
                else:
                    if abs(z) < 0.5:
                        del pos[(a, b)]
                        episode_count += 1
                    else:
                        d = st["dir"]
                        W.loc[t, a] = -d * per_pair / n_pairs
                        W.loc[t, b] = d * per_pair / n_pairs
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb_dates],
               "n_episodes": episode_count, "pool_min": pool_min}


# ------------------------------------------------------------- Eq_meanrev_cluster
def build_cluster(panel, cal, elig, theta=0.6, f_days=5, corr_win=126):
    reb_dates = [d for d in month_ends(cal)
                 if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    all_reb = list(month_ends(cal))
    cum = panel["a"] / panel["a"].shift(f_days) - 1.0
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    cluster_sizes = []
    for k, D in enumerate(reb_dates):
        e = elig.loc[D]
        names = e[e].index
        if len(names) < 20:
            continue
        R = panel["r"].loc[:D].tail(corr_win)[names]
        C = R.corr(min_periods=int(0.8 * corr_win))
        clusters = []
        assigned = set()
        for tkr in names:
            if tkr in assigned:
                continue
            cl = [tkr]
            assigned.add(tkr)
            row = C[tkr]
            for j in names:
                if j not in assigned and pd.notna(row.get(j)) and row[j] >= theta:
                    cl.append(j)
                    assigned.add(j)
            clusters.append(cl)
        cluster_sizes.append(len(clusters))
        nxt = all_reb[all_reb.index(D) + 1] if k + 1 < len(reb_dates) else cal[-1]
        span = cal[(cal > D) & (cal <= pd.Timestamp(nxt))]
        cl_of = {t: i for i, cl in enumerate(clusters) for t in cl}
        cl_arr = np.array([cl_of.get(t, -1) for t in panel["tickers"]])
        for t in span:
            i = cal.get_loc(t)
            dec = cal[i - 1]
            row = cum.loc[dec]
            valid = row.notna() & (cl_arr >= 0) & elig.loc[dec].values
            if valid.sum() < 10:
                continue
            r_tilde = pd.Series(np.nan, index=panel["tickers"])
            r_tilde[valid] = row[valid]
            # demean within cluster
            for cl in set(cl_arr[valid]):
                mem = valid & (cl_arr == cl)
                r_tilde[mem] -= r_tilde[mem].mean()
            mag = r_tilde.abs().sum()
            if mag > 0:
                W.loc[t] = (-r_tilde / mag).fillna(0.0)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb_dates],
               "n_episodes": len(reb_dates), "cluster_sizes": cluster_sizes}


# ------------------------------------------------------------- Eq_two_ma
def build_two_ma(panel, cal, tickers=("SPY",), t0=10, t1=30, cross_sectional=False,
                 elig=None):
    a = panel["a"]
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"] if cross_sectional else [tickers[0]])
    if cross_sectional:
        ma0 = a.rolling(t0, min_periods=t0).mean()
        ma1 = a.rolling(t1, min_periods=t1).mean()
        sig = np.sign(ma0 - ma1)
        sig = sig.where(elig)
        gross = sig.abs().sum(axis=1)
        sig = sig.div(gross.where(gross > 0), axis=0)
        W[:] = sig.shift(1).fillna(0.0)
        W = W.fillna(0.0)
        return W, {"n_episodes": int((np.diff((sig.fillna(0) != 0).any(axis=1).astype(int)) == 1).sum())}
    col = tickers[0]
    s = a[col]
    ma0 = s.rolling(t0, min_periods=t0).mean()
    ma1 = s.rolling(t1, min_periods=t1).mean()
    pos = np.sign(ma0 - ma1).shift(1).fillna(0.0)   # decision at close t-1
    W[col] = pos
    flips = int((pos.diff().abs() > 0).sum())
    return W, {"n_episodes": flips}


# ------------------------------------------------------------- Eq_sector_mom
def build_sector(panel, cal, pool, f_months=12, ma_filter=False, dual=False,
                 ma_len=200, fallback="TLT", rank_pool=None):
    rank_pool = list(rank_pool or pool)
    M = monthly_returns(panel)[pool]
    reb_dates = [d for d in month_ends(cal)
                 if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    wdates = []
    W = pd.DataFrame(0.0, index=cal, columns=pool)
    a = panel["a"][pool]
    spy = panel["a"]["SPY"]
    spy_ma = spy.rolling(ma_len, min_periods=ma_len).mean()
    for D in reb_dates:
        key = str(pd.Timestamp(D).date())
        mi = M.index.get_indexer([pd.Timestamp(D) + pd.offsets.MonthEnd(0)], method="nearest")
        m = M.index.get_loc(M.index[np.argmin(np.abs(M.index - pd.Timestamp(D)))])
        if m < f_months:
            continue
        form = M.iloc[m - f_months: m]           # f complete months
        ok = form.notna().all()
        sig = ((form + 1.0).prod() - 1.0)[ok]
        sig = sig[sig.index.isin(rank_pool)]
        if len(sig) < 4:
            continue
        s = sig.sort_values(ascending=False)
        if dual:
            if pd.isna(spy_ma.loc[D]) or spy.loc[D] <= spy_ma.loc[D]:
                w = {fallback: 1.0}
            else:
                top = list(s.index[:2])
                bot = list(s.index[-2:])
                w = {t: 0.25 for t in top}
                w.update({t: -0.25 for t in bot})
        else:
            top = list(s.index[:2])
            bot = list(s.index[-2:])
            w = {}
            if ma_filter:
                ma = a.rolling(ma_len, min_periods=ma_len).mean().loc[D]
                for t in top:
                    if pd.notna(ma.get(t)) and a.loc[D, t] > ma[t]:
                        w[t] = 0.25
                for t in bot:
                    if pd.notna(ma.get(t)) and a.loc[D, t] < ma[t]:
                        w[t] = -0.25
            else:
                w = {t: 0.25 for t in top}
                w.update({t: -0.25 for t in bot})
        if w:
            wdates[key] = w
    apply_rebalance(W, cal, reb_dates, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb_dates],
               "n_episodes": len(wdates), "pool": list(pool)}


# ------------------------------------------------------------- Eq_ibs_meanrev
def build_ibs(panel, cal, pool, k=4, volw=False):
    rng = (panel["h"][pool] - panel["l"][pool])
    ibs = ((panel["c"][pool] - panel["l"][pool]) / rng.replace(0.0, np.nan))
    W = pd.DataFrame(0.0, index=cal, columns=pool)
    sig_hist = ibs.shift(1)                       # decision at close t-1 uses day t-1
    sig_hist = sig_hist.where(panel["a"][pool].notna().cumsum() >= 60)
    sigma = panel["r"][pool].rolling(60, min_periods=40).std(ddof=1).shift(1)
    for t in cal:
        row = sig_hist.loc[t].dropna()
        if len(row) < 2 * k:
            continue
        s = row.sort_values(ascending=False, kind="mergesort")
        shorts = s.index[:k]
        longs = s.index[-k:]
        if volw:
            leg_names = pd.Index(list(longs) + list(shorts))
            inv = (1.0 / sigma.loc[t])[leg_names].replace([np.inf], np.nan).fillna(0.0)
            li = inv[longs] / inv[longs].sum() if inv[longs].sum() > 0 else None
            si = inv[shorts] / inv[shorts].sum() if inv[shorts].sum() > 0 else None
            if li is not None:
                for t2, v in li.items():
                    W.loc[t, t2] = 0.5 * v
            if si is not None:
                for t2, v in si.items():
                    W.loc[t, t2] = -0.5 * v
        else:
            for t2 in longs:
                W.loc[t, t2] = 0.5 / k
            for t2 in shorts:
                W.loc[t, t2] = -0.5 / k
    return W, {"n_episodes": len(cal), "pool": list(pool)}


# ------------------------------------------------------------- Eq_alpha101_batch
def _op_frames(panel):
    a, o, h, l, c, v = panel["a"], panel["o"], panel["h"], panel["l"], panel["c"], panel["v"]
    f = {
        "returns": panel["r"],
        "open": o, "high": h, "low": l, "close": c, "volume": v,
        "vwap": (o + h + l + c) / 4.0,
        "adv20": (c * v).rolling(20, min_periods=15).mean(),
        "log_volume": np.log(v.where(v > 0)),
    }
    return f


def _ts_argmax(df, d):
    """days since the max within the trailing d-bar window (0 = today)."""
    vals = df.values
    out = np.full(vals.shape, np.nan)
    n, m = vals.shape
    for j in range(m):
        col = vals[:, j]
        for i in range(d - 1, n):
            w = col[i - d + 1: i + 1]
            if np.isnan(w).any():
                continue
            out[i, j] = float(d - 1 - int(np.argmax(w)))
    return pd.DataFrame(out, index=df.index, columns=df.columns)


def _cond(cond, a, b):
    return pd.DataFrame(np.where(cond.fillna(False), a, b), index=cond.index,
                        columns=cond.columns)


def alpha_frame(n, f):
    R = lambda x: x.rank(axis=1, pct=True)
    delay = lambda x, d: x.shift(d)
    delta = lambda x, d: x - x.shift(d)
    SM = lambda x, d: x.rolling(d, min_periods=d).sum()
    tsmx = lambda x, d: x.rolling(d, min_periods=d).max()
    tsmn = lambda x, d: x.rolling(d, min_periods=d).min()
    tsr = lambda x, d: x.rolling(d, min_periods=d).rank(pct=True)
    corr = lambda x, y, d: x.rolling(d, min_periods=d).corr(y)
    cov = lambda x, y, d: x.rolling(d, min_periods=d).cov(y)
    sd = lambda x, d: x.rolling(d, min_periods=d).std(ddof=1)
    spow = lambda x, y: np.sign(x) * np.abs(x) ** y
    ret, o, h, l, c, v, vw, adv, lv = (f["returns"], f["open"], f["high"], f["low"],
                                       f["close"], f["volume"], f["vwap"], f["adv20"],
                                       f["log_volume"])
    if n == 1:
        x = _cond(ret < 0, sd(ret, 20), c)
        return R(_ts_argmax(spow(x, 2.0), 5)) - 0.5
    if n == 2:
        return -1 * corr(R(delta(lv, 2)), R((c - o) / o), 6)
    if n == 3:
        return -1 * corr(R(o), R(v), 10)
    if n == 4:
        return -1 * tsr(R(l), 9)
    if n == 5:
        return R(o - SM(vw, 10) / 10) * (-1 * R(c - vw).abs())
    if n == 6:
        return -1 * corr(o, v, 10)
    if n == 7:
        return _cond(adv < v, -1 * tsr(delta(c, 7).abs(), 60) * np.sign(delta(c, 7)), -1.0)
    if n == 8:
        x = SM(o, 5) * SM(ret, 5)
        return -1 * R(x - delay(x, 10))
    if n == 9:
        dc = delta(c, 1)
        return _cond(tsmn(dc, 5) > 0, dc, _cond(tsmx(dc, 5) < 0, dc, -1 * dc))
    if n == 10:
        dc = delta(c, 1)
        return R(_cond(tsmn(dc, 4) > 0, dc, _cond(tsmx(dc, 4) < 0, dc, -1 * dc)))
    if n == 11:
        return (R(tsmx(vw - c, 3)) + R(tsmn(vw - c, 3))) * R(delta(v, 3))
    if n == 12:
        return np.sign(delta(v, 1)) * (-1 * delta(c, 1))
    if n == 13:
        return -1 * R(cov(R(c), R(v), 5))
    if n == 14:
        return (-1 * R(delta(ret, 3))) * corr(o, v, 10)
    if n == 15:
        return -1 * SM(R(corr(R(h), R(v), 3)), 3)
    if n == 16:
        return -1 * R(cov(R(h), R(v), 5))
    if n == 17:
        return (-1 * R(tsr(c, 10))) * R(delta(delta(c, 1), 1)) * R(tsr(v / adv, 5))
    if n == 18:
        return -1 * R(sd((c - o).abs(), 5) + (c - o) + corr(c, o, 10))
    if n == 19:
        return -1 * np.sign((c - delay(c, 7)) + delta(c, 7)) * (1 + R(1 + SM(ret, 250)))
    if n == 20:
        return (-1 * R(o - delay(h, 1))) * R(o - delay(c, 1)) * R(o - delay(l, 1))
    raise ValueError(n)


def build_alpha(panel, cal, elig, n, top_frac=0.3):
    f = _op_frames(panel)
    alpha = alpha_frame(n, f)
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    a_lag = alpha.shift(1)                        # decision at close t-1
    e_lag = elig.shift(1).fillna(False).astype(bool)
    for t in cal:
        row = a_lag.loc[t]
        e = e_lag.loc[t]
        s = row[e & row.notna()].dropna()
        if len(s) < 10:
            continue
        ranks = s.rank(pct=True)
        n_leg = max(1, int(round(top_frac * len(s))))
        longs = ranks[ranks >= 1.0 - top_frac].index
        shorts = ranks[ranks <= top_frac].index
        for t2 in longs:
            W.loc[t, t2] = 1.0 / (2.0 * n_leg)
        for t2 in shorts:
            W.loc[t, t2] = -1.0 / (2.0 * n_leg)
    return W, {"n_episodes": int((W != 0).any(axis=1).sum()),
               "warmup_note": "Alpha#19 needs 250-bar sum; tape depth 2014-04"}


TUNE_LO_G, TUNE_HI_G = "2015-01-01", "2017-12-31"
