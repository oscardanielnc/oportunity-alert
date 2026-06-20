#!/usr/bin/env python3
"""
Momentum POR TICKER — para el proyecto tvindicators (señales de dirección para trades rápidos
en Binance). Sobre la caché 1m Feb-Jun: por cada ticker, edge de continuación tras salto
>= THR% anormal/30m + RVOL, forward H min, neto de fee Binance (0.05%), split UP/DOWN.
Marca si el ticker es operable en Binance perp.

⚠️ Muestra por-ticker es chica -> riesgo de sobreajuste. Usar como CANDIDATOS a re-validar en
tvindicators, no como verdad. READ-ONLY. Uso: python research/momentum_per_ticker.py
"""
from __future__ import annotations
import os, sys, glob
import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
CACHE = os.path.join(_ROOT, "data", "bars_cache")
LOOKBACK, RVOL_MIN, H, COOLDOWN, BENCH = 30, 2.0, 120, 30, "QQQ"
FEE = 0.05   # Binance perp round-trip aprox
THRS = [4.0, 5.0]

try:
    import ccxt
    _mk = ccxt.binanceusdm({"enableRateLimit": True}).load_markets()
    def perp(t): return f"{t}/USDT:USDT" in _mk
except Exception:
    def perp(t): return False


def load(sym):
    p = os.path.join(CACHE, f"{sym}_1m.parquet")
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    if df.empty:
        return None
    df = df[["c", "v"]].copy()
    df["etday"] = (df.index - pd.Timedelta(hours=4)).date
    return df


def ticker_trades(tk, qc):
    df = load(tk)
    if df is None or len(df) < 100:
        return []
    df["qc"] = qc.reindex(df.index, method="ffill")
    df = df.dropna(subset=["qc"])
    out = []
    for day, g in df.groupby("etday"):
        c, q, v = g["c"].values, g["qc"].values, g["v"].values
        n = len(g); mv = np.median(v[v > 0]) if (v > 0).any() else 0
        last = -10**9
        for t in range(LOOKBACK, n):
            if t - last < COOLDOWN or mv <= 0 or t + H >= n:
                continue
            abn = (c[t]/c[t-LOOKBACK]-1)*100 - (q[t]/q[t-LOOKBACK]-1)*100
            if v[t-LOOKBACK:t].sum()/(mv*LOOKBACK) < RVOL_MIN or abs(abn) < min(THRS):
                continue
            last = t
            sign = 1 if abn > 0 else -1
            fwd = sign*(c[t+H]/c[t]-1)*100 - FEE
            out.append((abs(abn), "UP" if abn > 0 else "DOWN", fwd))
    return out


def main():
    qc = load(BENCH)["c"]
    tickers = [os.path.basename(f).replace("_1m.parquet", "") for f in sorted(glob.glob(os.path.join(CACHE, "*_1m.parquet"))) if BENCH not in f]
    rows = []
    for tk in tickers:
        tr = ticker_trades(tk, qc)
        for thr in THRS:
            sub = [x for x in tr if x[0] >= thr]
            if len(sub) < 8:
                continue
            f = np.array([x[2] for x in sub])
            up = [x[2] for x in sub if x[1] == "UP"]; dn = [x[2] for x in sub if x[1] == "DOWN"]
            rows.append({"ticker": tk, "thr": thr, "n": len(sub),
                         "fwd_net": round(f.mean(), 3), "win": round(100*(f > 0).mean()),
                         "up_n": len(up), "up_fwd": round(np.mean(up), 2) if up else None,
                         "dn_n": len(dn), "dn_fwd": round(np.mean(dn), 2) if dn else None,
                         "perp": perp(tk)})

    for thr in THRS:
        sub = sorted([r for r in rows if r["thr"] == thr], key=lambda r: -r["fwd_net"])
        print(f"\n===== THR {thr:.0f}% — momentum por ticker (fwd neto fee {FEE}%, H={H}m, n>=8) =====")
        print(f"{'ticker':<7}{'n':>4}{'fwd_net%':>9}{'win%':>6}  {'UP(n/fwd)':>14}  {'DOWN(n/fwd)':>14}  perp")
        for r in sub:
            up = f"{r['up_n']}/{r['up_fwd']:+.1f}" if r['up_fwd'] is not None else "-"
            dn = f"{r['dn_n']}/{r['dn_fwd']:+.1f}" if r['dn_fwd'] is not None else "-"
            tag = "✓PERP" if r["perp"] else ""
            print(f"{r['ticker']:<7}{r['n']:>4}{r['fwd_net']:>+9.2f}{r['win']:>5}%  {up:>14}  {dn:>14}  {tag}")

    # candidatos: positivos consistentes (>=5%, fwd_net>0, n>=10)
    cand = [r for r in rows if r["thr"] == 5.0 and r["fwd_net"] > 0 and r["n"] >= 10]
    cand.sort(key=lambda r: -r["fwd_net"])
    print(f"\n===== CANDIDATOS para tvindicators (>=5%, fwd_net>0, n>=10) =====")
    print("PERP (operables en Binance):", ", ".join(r["ticker"] for r in cand if r["perp"]) or "(ninguno)")
    print("NO perp (solo eToro/mercado):", ", ".join(r["ticker"] for r in cand if not r["perp"]) or "(ninguno)")


if __name__ == "__main__":
    main()
